#!/usr/bin/env python3
"""vault-lint: report, fill and rotate for the second-brain vault (schema: <vault>/AGENTS.md).

  vault_lint.py report [--json]          lint every .md (counts + first 20 offenders per rule)
  vault_lint.py fill --dry-run|--apply   complete frontmatter on wiki pages, no LLM
  vault_lint.py rotate --dry-run|--apply [--days 30] [--what all|history|logs]
      history: history.md > 300 KB moves entries older than --days to _archive/history-<lo>-to-<hi>.md
      logs:    errors/lessons/decisions.md > 100 KB move their oldest dated sections to _archive/<stem>-<YYYY>.md
  vault_lint.py --self-test
Stdlib only. --vault overrides the default vault path.
"""
import argparse, contextlib, datetime as dt, fcntl, hashlib, json, os, re, shutil, subprocess, sys, tempfile, time
from collections import Counter, defaultdict

VAULT = __import__('os').path.expanduser('~/AI_Projects/_system/second-brain')
HANDOFF_LOCKS = os.path.join(os.environ.get('HANDOFF_STATE', __import__('os').path.expanduser('~/AI_Projects/pipeline/handoff-v2/state')), 'locks')
LOCK_TIMEOUT = 60  # seconds rotate waits for a running distill before skipping the file
KB = 1024
STANDARD = {n.lower() for n in ('NOTES.md history.md decisions.md lessons.md errors.md RESUME.md README.md '
            'open-loops.md next-prompt.md index.md log.md state.md status.md _brief.md prefs.md tokens.md '
            'resume-overflow.md resume-closed.md').split()}  # handoff-v2 render._archive, one each per project
CONTROL = {'AGENTS.md', 'CLAUDE.md', 'MEMORY.md', 'boot.md'}  # schema/boot/generated files: own budget rules, no frontmatter
FIELDS = ('type', 'project', 'status', 'updated', 'description')
DATED = re.compile(r'^## (\d{4}-\d{2}-\d{2})\b')
JOBLINE = re.compile(r'^<!-- job:[^>]*-->\s*$')  # standalone replay marker render.errors_block writes above a job
LESSON_DATE = re.compile(r'_\((\d{4}-\d{2}-\d{2}), session ')  # the stamp render.lessons_block puts on each bullet
SECTION = re.compile(r'^(?=## \d{4}-\d{2}-\d{2}\b|<!-- job:[^>]*-->[ \t\r]*$)', re.M)  # cut points of the no-loss check
LOGS = ('errors.md', 'lessons.md', 'decisions.md')  # append-style wiki logs handoff-v2 writes (render._append_once)
LOG_MAX, LOG_TARGET = 100 * KB, 70 * KB
ROTATED_LOG = re.compile(r'^(errors|lessons|decisions)-\d{4}\.md$')  # one per project by design, not a duplicate
LINK = re.compile(r'!?\[\[([^\]\n]+?)\]\]')


def walk(vault, dirs_too=False):
    """Yield (relpath, abspath) for every file, skipping dot dirs/files (.git, .sync, .obsidian, shared logs)."""
    for root, dirs, files in os.walk(vault):
        dirs[:] = sorted(d for d in dirs if not d.startswith('.'))
        for f in sorted(files + (dirs if dirs_too else [])):
            if not f.startswith('.'):
                p = os.path.join(root, f)
                yield os.path.relpath(p, vault), p


def is_raw(rel):
    parts = rel.split(os.sep)
    name = parts[-1]
    return (name in ('history.md', 'journal.ndjson', 'resume-overflow.md', 'resume-closed.md') or 'sessions' in parts[:-1]
            or (name.startswith('history-') and '_archive' in parts))  # rotated history stays raw


def is_memory(rel):
    return rel.split(os.sep)[0] == 'harness-memory'


def split_fm(text):
    """-> (frontmatter lines or None, body)."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].lstrip('﻿').strip() != '---':
        return None, text
    for i in range(1, len(lines)):
        if lines[i].strip() in ('---', '...'):
            return lines[1:i], ''.join(lines[i + 1:])
    return None, text  # ponytail: unclosed '---' is treated as no frontmatter


def parse_fm(fm):
    d, parent = {}, None
    for ln in fm or ():
        m = re.match(r'^(\s*)([A-Za-z_][\w.-]*)\s*:\s*(.*?)\s*$', ln.rstrip('\r\n'))
        if not m:
            continue
        ind, k, v = m.groups()
        v = v.strip('"\'')
        if not ind:
            d[k], parent = v, k
        elif parent:
            d[f'{parent}.{k}'] = v  # one nesting level is enough for metadata.type
    return d


def read(p):
    with open(p, encoding='utf-8', errors='replace', newline='') as f:
        return f.read()


def report(vault, today=None):
    today = today or dt.date.today()
    hits = defaultdict(list)
    files = list(walk(vault))
    # a [[link]] resolves to any file or folder name: the distiller links project folders as [[<project>]]
    names = {os.path.basename(rel).lower() for rel, _ in walk(vault, dirs_too=True)}
    by_base = defaultdict(list)
    for rel, p in files:
        base = os.path.basename(rel).lower()
        if base.endswith('.md'):
            names.add(base[:-3])
            if is_memory(rel):  # memories link each other by their name: slug, not the filename
                names.add(parse_fm(split_fm(read(p))[0]).get('name', '').lower())
            if base not in STANDARD and not ROTATED_LOG.match(base):
                by_base[base].append(rel)
    for rel, p in files:
        name, size = os.path.basename(rel), os.path.getsize(p)
        raw = is_raw(rel)
        if raw and size > 300 * KB and not name.startswith('history-'):
            hits['raw_over_300kb'].append(f'{rel} ({size // KB} KB)')
        if not name.endswith('.md'):
            continue
        if not raw and size > 100 * KB and not (ROTATED_LOG.match(name) and '_archive' in rel.split(os.sep)):  # a year's rotated log grows by design
            hits['page_over_100kb'].append(f'{rel} ({size // KB} KB)')
        text = read(p)
        fm_lines, body = split_fm(text)
        fm = parse_fm(fm_lines)
        if name in CONTROL:
            n_lines = text.count('\n')
            if name == 'MEMORY.md' and (n_lines > 200 or size > 25 * KB):
                hits['memory_index_over_budget'].append(f'{rel} ({n_lines} lines, {size // KB} KB)')
            if name == 'boot.md' and len(text) > 4000:
                hits['boot_over_budget'].append(f'{rel} ({len(text)} chars)')
            if name == 'AGENTS.md' and n_lines > 120:
                hits['agents_over_budget'].append(f'{rel} ({n_lines} lines)')
        elif is_memory(rel):
            miss = [k for k in ('name', 'description', 'metadata.type') if not fm.get(k)]
            if miss:
                hits['memory_missing_fields'].append(f'{rel} (missing {", ".join(miss)})')
        elif not raw:
            if fm_lines is None:
                hits['missing_frontmatter'].append(rel)
            else:
                if not fm.get('type'):
                    hits['missing_type'].append(rel)
                if not fm.get('description'):
                    hits['missing_description'].append(rel)
        if fm.get('status') == 'active':
            try:
                upd = dt.date.fromisoformat(fm.get('updated', '')[:10])
                if (today - upd).days > 90:
                    hits['stale_active'].append(f'{rel} (updated {upd})')
            except ValueError:
                pass
        clean = re.sub(r'`[^`\n]*`', '', re.sub(r'```.*?```', '', body, flags=re.S))
        for t in sorted({m.split('|')[0].split('#')[0].rstrip('\\').strip() for m in LINK.findall(clean)}):
            if t and t.rsplit('/', 1)[-1].lower() not in names:
                hits['dead_links'].append(f'{rel} -> [[{t}]]')
    for base, rels in sorted(by_base.items()):
        if len(rels) > 1:
            hits['duplicate_basenames'].append(f'{base}: ' + ', '.join(rels))
    rules = ('missing_frontmatter missing_type missing_description memory_missing_fields page_over_100kb '
             'raw_over_300kb dead_links stale_active duplicate_basenames memory_index_over_budget '
             'boot_over_budget agents_over_budget').split()
    return {'vault': vault, 'date': str(today), 'pages': sum(r.endswith('.md') for r, _ in files),
            'counts': {r: len(hits[r]) for r in rules}, 'offenders': {r: hits[r] for r in rules}}


def git_date(vault, rel):
    try:
        out = subprocess.run(['git', '-C', vault, 'log', '-1', '--format=%cs', '--', rel],
                             capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        out = ''
    return out or str(dt.date.today())


def guess(rel, body, vault, fm):
    parts = rel.split(os.sep)
    name = parts[-1]
    t = {'decisions.md': 'decision', 'lessons.md': 'lesson', 'errors.md': 'note'}.get(name)
    if not t:
        if 'Reference' in parts and 'lane-intel' in parts:
            t = 'lane-intel'
        else:
            t = next((v for k, v in (('Reference', 'reference'), ('Actions', 'action'), ('Research', 'research'),
                                     ('weekly', 'weekly')) if k in parts[:-1]), 'note')
    proj = 'system'
    if parts[0] == 'Projects' and len(parts) > 2:
        proj = parts[2] if parts[1] in ('_archive', '_parked') and len(parts) > 3 else parts[1]
    elif parts[:2] == ['_archive', 'projects'] and len(parts) > 3:
        proj = parts[2]  # dead projects move whole to _archive/projects/<p>/ (AGENTS.md)
    # ponytail: spec says status active; archived/parked pages get done/parked so stale_active stays meaningful
    status = 'done' if '_archive' in parts else 'parked' if '_parked' in parts else 'active'
    lines = [ln.strip() for ln in body.splitlines() if ln.strip() and ln.strip() != '---']
    first = next((ln for ln in lines if ln.startswith('#')), lines[0] if lines else '')
    desc = (fm.get('summary') or fm.get('title') or re.sub(r'^[#>\s]+', '', first).replace('**', '').strip()
            or os.path.splitext(name)[0])  # existing summary:/title: beat a heading
    return {'type': t, 'project': proj, 'status': status, 'updated': git_date(vault, rel),
            'description': desc[:120]}


def fill(vault, apply):
    changed, skipped, diffs = [], [], []
    for rel, p in walk(vault):
        if (not rel.endswith('.md') or is_raw(rel) or is_memory(rel) or os.path.basename(rel) in CONTROL):
            continue
        try:
            with open(p, encoding='utf-8', newline='') as f:
                text = f.read()
        except UnicodeDecodeError:
            skipped.append(rel)
            continue
        fm_lines, body = split_fm(text)
        fm = parse_fm(fm_lines)
        if fm_lines is not None and fm.get('type') and fm.get('description'):
            continue
        nl = '\r\n' if '\r\n' in text[:2000] else '\n'
        vals = guess(rel, body, vault, fm)
        add = [f'{k}: {json.dumps(vals[k], ensure_ascii=False) if k == "description" else vals[k]}'
               for k in FIELDS if not fm.get(k)]
        if fm_lines is None:
            new = '---' + nl + nl.join(add) + nl + '---' + nl + nl + text.lstrip('﻿')
        else:
            n_open = 1 + len(fm_lines)  # line index of the closing '---'
            lines = text.splitlines(keepends=True)
            lines[n_open:n_open] = [a + nl for a in add]
            new = ''.join(lines)
        changed.append(rel)
        if len(diffs) < 10:
            diffs.append(f'--- {rel} ({"new block" if fm_lines is None else "completed"})\n' + '\n'.join('+ ' + a for a in add))
        if apply:
            tmp = p + '.vlint.tmp'
            with open(tmp, 'w', encoding='utf-8', newline='') as f:
                f.write(new)
            shutil.copymode(p, tmp)
            os.replace(tmp, p)
    return changed, skipped, diffs


@contextlib.contextmanager
def distiller_lock(rel):
    """Hold handoff-v2's own per-project flock (store.project_lock) while rotating Projects/<p>/history.md.
    The distiller rewrites history.md whole (read, append, atomic replace) under that lock, so without it a
    concurrent distill either loses its new entry or resurrects the rotated ones."""
    parts = rel.split(os.sep)
    lock = (os.path.join(HANDOFF_LOCKS, hashlib.sha256(f'project:{parts[1]}'.encode()).hexdigest()[:32] + '.lock')
            if len(parts) == 3 and parts[0] == 'Projects' else '')
    if not os.path.exists(lock):
        yield  # the distiller has never written this project on this machine
        return
    fd = os.open(lock, os.O_RDONLY)  # read-only open: creates nothing outside the vault
    try:
        deadline = time.monotonic() + LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError
                time.sleep(0.1)
        yield
    finally:
        os.close(fd)  # closing the fd releases the flock


def rotate(vault, apply, days=30, today=None, what='all'):
    today = today or dt.date.today()
    cutoff = today - dt.timedelta(days=days)
    out = []
    for rel, p in walk(vault):
        parts, size = rel.split(os.sep), os.path.getsize(p)
        if what in ('all', 'history') and parts[-1] == 'history.md' and size > 300 * KB:
            job = lambda: _rotate_one(vault, rel, p, apply, cutoff)
        elif (what in ('all', 'logs') and parts[-1] in LOGS and size > LOG_MAX
              and len(parts) == 3 and parts[0] == 'Projects'):  # Projects/<p>/<log>.md; archives never match
            job = lambda: _rotate_log(vault, rel, p, apply, today)
        else:
            continue
        try:
            with distiller_lock(rel) if apply else contextlib.nullcontext():
                out.append(job())
        except TimeoutError:
            out.append({'file': rel, 'skipped': f'handoff-v2 project lock busy for {LOCK_TIMEOUT} s'})
    return out


def split_units(text):
    """-> (header, [(date, text)]). A unit starts at a '## YYYY-MM-DD' heading and owns everything up to the next
    one (undated '## ' subsections included). A standalone '<!-- job:<id> -->' line also starts a unit:
    render.errors_block writes it above a job's first section, which joins it, and the job's further sections
    (same date, no marker of their own) join too, so the replay marker always travels with every section of its
    job. render.lessons_block writes the marker above bare bullets: that block is a unit dated by its first
    '_(YYYY-MM-DD, session ' stamp (else the unit before it); undated blocks before any date stay in the header.
    Invariant: header + ''.join(unit texts) == text."""
    header, units = [], []  # units: [date or None, lines, opened_by_standalone_marker, has_dated_heading]
    for ln in text.splitlines(keepends=True):
        if JOBLINE.match(ln):
            units.append([None, [ln], True, False])
            continue
        m = DATED.match(ln)
        u = units[-1] if units else None
        if not m:
            (u[1] if u else header).append(ln)
        elif u and u[2] and not u[3] and not any(x.strip() for x in u[1][1:]):
            u[0], u[3] = m.group(1), True  # the job's first section, under its marker
            u[1].append(ln)
        elif u and u[2] and u[3] and u[0] == m.group(1) and 'job:' not in ln:
            u[1].append(ln)  # the job's further sections
        else:
            units.append([m.group(1), [ln], False, True])
    prev = None
    for u in units:
        if u[0] is None:
            dm = LESSON_DATE.search(''.join(u[1]))
            u[0] = dm.group(1) if dm else prev
        prev = u[0]
    while units and units[0][0] is None:
        header += units.pop(0)[1]
    # ponytail: '## YYYY-MM-DD' inside a fenced block would split a unit; no log in the vault has one
    out = ''.join(header), [(d, ''.join(ls)) for d, ls, _, _ in units]
    assert out[0] + ''.join(t for _, t in out[1]) == text
    return out


def bag(groups):
    """Multiset of sections over groups of units, cut at every dated heading and job marker: per section, not per
    unit, so a date-sorted merge that regroups units (a marker-less same-date section joins the job before it)
    reads neither as a loss nor as a new section."""
    c = Counter()
    for g in groups:
        for _, t in g:
            c.update(sec for sec in SECTION.split(_nl(t)) if sec)
    return c


def secs(t):
    """Sections of a whole page (cut as in bag), leaving out only its preamble, the text before the first cut.
    Counting pages, not split_units' units, keeps the no-loss check independent of where split_units draws the
    header: an unstamped marker block left first on the live page re-parses into the header, yet is still there."""
    return Counter(SECTION.split(_nl(t))[1:])


def _nl(t):
    return t if t.endswith('\n') else t + '\n'


def _atomic(path, text, mode_from=None):
    tmp = path + '.vlint.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='') as f:
        f.write(text)
    if mode_from and os.path.exists(mode_from):
        shutil.copymode(mode_from, tmp)
    os.replace(tmp, path)


def _rotate_log(vault, rel, p, apply, today):
    """Move the oldest dated units of an append-style log into _archive/<stem>-<YYYY>.md until the live page
    is <= LOG_TARGET. Text is only moved: the multiset of sections (live + touched archives, whole pages cut at
    every dated heading and job marker, so it does not depend on how split_units groups them or where it puts the
    header) is checked equal before anything is written and again after, reading back from disk. A unit already
    in its archive verbatim is left out instead of copied twice: an interrupted run (archive written, live page
    not) recovers on the next run."""
    with open(p, encoding='utf-8', newline='') as f:
        text = f.read()
    header, units = split_units(text)
    size = len(text.encode())
    rec = {'file': rel, 'size_kb': size // KB, 'entries': len(units), 'move': 0, 'size_kb_after': size // KB}
    move, cur = set(), size
    for i in sorted(range(len(units)), key=lambda i: (units[i][0], i)):  # oldest date first, file order on ties
        if cur <= LOG_TARGET or len(move) == len(units) - 1:  # always keep the newest unit live
            break
        move.add(i)
        cur -= len(units[i][1].encode())
    if not move:
        rec['skipped'] = 'no dated sections to move' if not units else 'nothing to move'
        return rec
    stem, proj = os.path.splitext(os.path.basename(rel))[0], rel.split(os.sep)[1]
    ftype = parse_fm(split_fm(text)[0]).get('type') or 'note'
    live = header + ''.join(t for i, (_, t) in enumerate(units) if i not in move)
    by_year = defaultdict(list)
    for i in sorted(move):
        by_year[units[i][0][:4]].append(units[i])
    before, after, writes, dups = secs(text), secs(live), [(p, live)], []
    rec.update(move=len(move), size_kb_after=len(live.encode()) // KB, archives={})
    for year, moved in sorted(by_year.items()):
        arc_rel = os.path.join(os.path.dirname(rel), '_archive', f'{stem}-{year}.md')
        arc = os.path.join(vault, arc_rel)
        a_old = ''
        if os.path.exists(arc):
            with open(arc, encoding='utf-8', newline='') as f:
                a_old = f.read()
            a_head, a_units = split_units(a_old)
            a_head = re.sub(r'^updated:.*$', f'updated: {today}', a_head, count=1, flags=re.M)
        else:
            a_head, a_units = (f'---\ntype: {ftype}\nproject: {proj}\nstatus: done\nupdated: {today}\n'
                               f'description: "{proj} {stem} from {year}, rotated out of the live page, oldest first"\n'
                               f'---\n\n# {proj} — {stem} archive {year}\n\n'
                               f'Oldest sections of `{rel}`, moved here verbatim by `vault-lint rotate` when the live '
                               f'page passed {LOG_MAX // KB} KB. New entries keep landing on the live page.\n\n'), []
        have, fresh = secs(a_old), []  # matched per section: the archive may have regrouped them by date
        for u in moved:
            need = bag([[u]])
            if all(have[s] >= n for s, n in need.items()):  # already archived by an interrupted run: keep that copy
                have -= need
                before -= need  # counted once, in its archive
                dups.append(u)
            elif any(have[s] for s in need):  # an interrupted run writes whole units: this archive was hand-edited
                raise RuntimeError(f'{rel}: a section dated {u[0]} is partly in {arc_rel} already; nothing written')
            else:
                fresh.append(u)
        merged = sorted(a_units + fresh, key=lambda u: u[0])  # stable: existing archive text first on equal dates
        a_text = _nl(a_head) + ''.join(_nl(t) for _, t in merged)
        before += secs(a_old)
        after += secs(a_text)
        writes.insert(0, (arc, a_text))  # archives first: a crash leaves a copy the next run drops, never a loss
        rec['archives'][arc_rel] = len(fresh)
    if dups:
        rec['already_archived'] = len(dups)
    if before != after or not live.startswith(header):
        raise RuntimeError(f'{rel}: rotation would not preserve every section; nothing written')
    if apply:
        for path, t in writes:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            _atomic(path, t, mode_from=p)
        back = Counter()
        for path, _ in writes:
            with open(path, encoding='utf-8', newline='') as f:
                back += secs(f.read())
        if back != before:
            raise RuntimeError(f'{rel}: post-write check failed; restore from git')
        rec['verified'] = True
    return rec


def _rotate_one(vault, rel, p, apply, cutoff):
    with open(p, encoding='utf-8', errors='replace', newline='') as f:
        lines = f.read().splitlines(keepends=True)
    header, entries = [], []  # entries: [date, lines]; undated '## ' sections ride with the entry above
    for ln in lines:
        m = DATED.match(ln)
        if m:
            entries.append([m.group(1), [ln]])
        elif entries:
            entries[-1][1].append(ln)
        else:
            header.append(ln)
    # ponytail: '## YYYY-MM-DD' inside a fenced block would split an entry; the distiller never writes one
    old = [e for e in entries if e[0] < str(cutoff)]
    keep = [e for e in entries if e[0] >= str(cutoff)]
    kept = ''.join(header) + ''.join(''.join(e[1]) for e in keep)
    rec = {'file': rel, 'size_kb': os.path.getsize(p) // KB, 'entries': len(entries), 'move': len(old),
           'size_kb_after': len(kept.encode()) // KB}
    if old:
        lo, hi = min(e[0] for e in old), max(e[0] for e in old)
        arc_rel = os.path.join(os.path.dirname(rel), '_archive', f'history-{lo}-to-{hi}.md')
        rec['archive'] = arc_rel
        if apply:
            arc = os.path.join(vault, arc_rel)
            os.makedirs(os.path.dirname(arc), exist_ok=True)
            new_file = not os.path.exists(arc)
            with open(arc, 'a', encoding='utf-8', newline='') as f:
                if new_file:
                    f.write(f'# {os.path.basename(os.path.dirname(p))} — history archive {lo} to {hi}\n\n')
                f.write(''.join(''.join(e[1]) for e in old))
            tmp = p + '.vlint.tmp'
            with open(tmp, 'w', encoding='utf-8', newline='') as f:
                f.write(kept)
            shutil.copymode(p, tmp)
            os.replace(tmp, p)
    return rec


def print_report(r):
    print(f"vault-lint {r['date']}  {r['pages']} pages  {r['vault']}")
    for rule, n in r['counts'].items():
        print(f'  {rule:26} {n}')
    for rule, offs in r['offenders'].items():
        if offs:
            print(f'\n## {rule} ({len(offs)})')
            for o in offs[:20]:
                print('  ' + o)


RENDER_ROOT = __import__('os').path.expanduser('~/AI_Projects/pipeline/handoff-v2')  # handoff-v2: the writer of the logs rotated here


def _w(v, rel, s):
    os.makedirs(os.path.dirname(os.path.join(v, rel)), exist_ok=True)
    with open(os.path.join(v, rel), 'w', encoding='utf-8', newline='') as f:
        f.write(s)


def astra3_5(v, today):
    """Astra round 3 item 5: crash recovery when the archive regrouped sections. The archive ends on job zz of
    2026-01-05; the oldest live section is a marker-less hand note of that day, which the merge joins into zz's
    unit. Interrupted after the archive write, the rerun must recognise the hand note inside that combined unit
    (match per section, not per unit) instead of appending a second copy."""
    body = lambda n: '**What:** ' + 'y' * 9000 + f' {n}\n\n'
    fm = '---\ntype: note\nproject: demo4\n---\n\n'
    log = (fm + '## 2026-01-05 — hand note, same day as the archived job\n' + body(50)
           + ''.join(f'<!-- job:k{d} -->\n## 2026-10-{d:02d} — job k{d}\n' + body(d + 50) for d in range(1, 12)))
    arc0 = fm + '<!-- job:zz -->\n## 2026-01-05 — zz\nbody\n'
    lp, ap = (os.path.join(v, 'Projects/demo4', x) for x in ('errors.md', '_archive/errors-2026.md'))
    _w(v, 'Projects/demo4/errors.md', log)
    _w(v, 'Projects/demo4/_archive/errors-2026.md', arc0)
    real, calls = _atomic, []

    def crash(path, text, mode_from=None):
        if path == lp:
            raise OSError('simulated crash')
        calls.append(path)
        real(path, text, mode_from)
    globals()['_atomic'] = crash
    try:
        rotate(v, apply=True, today=today, what='logs')
        raise AssertionError('crash not simulated')
    except OSError:
        pass
    finally:
        globals()['_atomic'] = real
    a = read(ap)
    assert calls == [ap] and read(lp) == log and a.index('job:zz') < a.index('hand note'), calls
    assert len(split_units(a)[1]) < len(split_units(log)[1]) and bag([split_units(a)[1]])  # regrouped in the archive
    r = rotate(v, apply=True, today=today, what='logs')[0]
    live, a = read(lp), read(ap)
    assert r['verified'] and r['already_archived'] == r['move'] and r['archives'] == {
        'Projects/demo4/_archive/errors-2026.md': 0}, r
    assert (live + a).count('hand note') == 1 and (live + a).count('job:zz') == 1, 'duplicate after recovery'
    want = bag([split_units(log)[1], split_units(arc0)[1]])
    assert bag([split_units(live)[1], split_units(a)[1]]) == want, 'sections lost or duplicated'
    # a unit only partly in its archive (not something a crash leaves) is refused, never doubled: nothing written
    _w(v, 'Projects/demo4b/errors.md', log.replace('demo4', 'demo4b'))
    part = fm.replace('demo4', 'demo4b') + '## 2026-10-01 — job k1\n' + body(51)  # k1's section without its marker
    _w(v, 'Projects/demo4b/_archive/errors-2026.md', part)
    try:
        rotate(v, apply=True, today=today, what='logs')
        raise AssertionError('partial archive copy not refused')
    except RuntimeError:
        pass
    assert read(os.path.join(v, 'Projects/demo4b/_archive/errors-2026.md')) == part
    shutil.rmtree(os.path.join(v, 'Projects/demo4b'))


def astra3_6(v, today):
    """Astra round 3 item 6: generated lesson logs rotate. render.lessons_block writes '<!-- job:<id> -->' over
    bare bullets stamped '_(YYYY-MM-DD, session <sid>)_', no '## date' heading. Each block must be one unit, dated
    by that stamp, moved oldest first with its marker. The fixture's blocks are checked byte-equal to what
    render._append_once writes for the same items (run in a subprocess on a temp vault and state)."""
    d0, jobs = dt.date(2025, 11, 20), []
    for j in range(60):
        meta = {'job_id': f'{j:020x}', 'session_id': f'{j:08x}deadbeef', 'date': str(d0 + dt.timedelta(days=3 * j)),
                'title': f't{j}'}
        jobs.append((meta, [{'rule': f'Rule {j}.{k} ' + 'r' * 200, 'evidence': f'evidence {j}.{k} ' + 'e' * 400}
                            for k in range(4)]))

    def block(meta, items):  # render.lessons_block, copied
        out = [f"<!-- job:{meta['job_id']} -->"]
        for les in items:
            lid = 'l-' + hashlib.sha256(les['rule'].strip().lower().encode()).hexdigest()[:8]
            out.append(f"- [candidate] **{les['rule']}** — evidence: \"{les['evidence'][:300]}\" "
                       f"_({meta['date']}, session {meta['session_id'][:8]})_ <!-- id:{lid} -->")
        return '\n'.join(out)
    text = ('---\ntype: lesson\nproject: demo5\nstatus: active\nupdated: 2026-05-16\ndescription: "d"\n---\n\n'
            '# demo5 — lessons\n\n**For future Claude:** candidate rules.\n')
    for meta, items in jobs:  # render._append_once
        text = text.rstrip('\n') + '\n\n' + block(meta, items).rstrip() + '\n'
    if os.path.exists(os.path.join(RENDER_ROOT, 'handoff', 'render.py')):
        with tempfile.TemporaryDirectory() as rv, tempfile.TemporaryDirectory() as rs:
            code = ('import json, sys\nfrom handoff import render, config as C\n'
                    'assert str(C.VAULT) == sys.argv[1] and str(C.STATE) == sys.argv[2]\n'
                    'for meta, items in json.load(sys.stdin):\n'
                    '    render._append_once("demo5", "lessons", f"<!-- job:{meta[\'job_id\']} -->", '
                    'render.lessons_block(items, meta), meta["date"])\n')
            env = dict(os.environ, HANDOFF_VAULT=rv, HANDOFF_STATE=rs, PYTHONPATH=RENDER_ROOT)
            subprocess.run([sys.executable, '-c', code, rv, rs], input=json.dumps(jobs), text=True, env=env,
                           cwd=rs, check=True, timeout=120)
            gen = read(os.path.join(rv, 'Projects/demo5/lessons.md'))
        assert gen[gen.index('<!-- job:'):] == text[text.index('<!-- job:'):], 'render.lessons_block format drifted'
    else:
        print(f'astra3_6: {RENDER_ROOT} missing, format-drift check skipped')
    # hand edits seen in the live vault: a later dated review note on a bullet, a block without a stamp
    text = text.replace(' <!-- id:', ' <!-- defaults-review 2026-10-02 --> <!-- id:', 1)
    text = text.replace('<!-- job:%020x -->\n' % 30, '<!-- job:%020x -->\n- hand note, no stamp\n' % 30, 1)
    lp = os.path.join(v, 'Projects/demo5/lessons.md')
    _w(v, 'Projects/demo5/lessons.md', text)
    hdr, units = split_units(text)
    assert hdr.endswith('candidate rules.\n\n') and len(units) == 60, (hdr[-60:], len(units))
    assert [u[0] for u in units] == [m['date'] for m, _ in jobs] and all(t.startswith('<!-- job:') for _, t in units)
    dry = rotate(v, apply=False, today=today, what='logs')
    assert len(dry) == 1 and dry[0]['entries'] == 60 and dry[0]['move'] > 0 and read(lp) == text, dry
    r = rotate(v, apply=True, today=today, what='logs')[0]
    assert r['verified'] and r['move'] == dry[0]['move'] and r['size_kb_after'] <= LOG_TARGET // KB, r
    files = [lp] + [os.path.join(v, a) for a in sorted(r['archives'])]
    assert [os.path.basename(f) for f in files] == ['lessons.md', 'lessons-2025.md', 'lessons-2026.md'], files
    texts = [read(f) for f in files]
    key = lambda t: sorted(ln for ln in t.splitlines() if ln.startswith(('- ', '<!-- job:')))
    assert key(''.join(texts)) == key(text), 'a lesson or marker was lost or duplicated'
    assert bag([split_units(t)[1] for t in texts]) == bag([units])
    moved = [m['job_id'] for m, _ in jobs][:r['move']]  # oldest first
    for f, t in zip(files, texts):
        blocks = re.findall(r'<!-- job:([0-9a-f]+) -->\n((?:- .*\n?)+)', t)
        assert blocks and all(f'Rule {int(j, 16)}.' in b or 'no stamp' in b
                              for j, b in blocks for b in b.splitlines()), f   # each marker over its own bullets
        ds = re.findall(r'_\((\d{4}-\d{2}-\d{2}), session', t)
        assert ds == sorted(ds) and (f == lp) == (ds[0] > jobs[r['move'] - 1][0]['date']), (f, ds[:1])
        if f != lp:
            assert {d[:4] for d in ds} == {os.path.basename(f)[8:12]} and all(j in moved for j, _ in blocks), f
    assert texts[0].startswith(hdr) and rotate(v, apply=False, today=today, what='logs') == []


def _crash_after(n_writes, fn):
    """Run fn with _atomic failing after n_writes successful writes (a simulated crash mid-rotation)."""
    real, n = _atomic, [0]

    def crash(path, text, mode_from=None):
        if n[0] == n_writes:
            raise OSError('simulated crash')
        n[0] += 1
        real(path, text, mode_from)
    globals()['_atomic'] = crash
    try:
        fn()
        raise AssertionError('crash not simulated')
    except OSError:
        pass
    finally:
        globals()['_atomic'] = real


def astra3_5b(v, today):
    """Astra round 3 item 5, review variant: both year archives regrouped (a marker-less hand note of the same
    day as each archive's last job), the run interrupted after the first and after the second archive write,
    and a distiller append landing on the live page before the rerun (the move set grows). No section may be
    lost or doubled across live + both archives."""
    body = lambda n: '**What:** ' + 'y' * 8000 + f' {n}\n\n'
    fm = '---\ntype: note\nproject: demo7\n---\n\n'
    log = (fm + '## 2025-12-30 — hand note A\n' + body(1) + '<!-- job:j25 -->\n## 2025-12-31 — job j25\n' + body(2)
           + '## 2026-01-05 — hand note B\n' + body(3)
           + ''.join(f'<!-- job:k{d} -->\n## 2026-{d:02d}-01 — job k{d}\n' + body(d + 50) for d in range(2, 13)))
    arcs = {'2025': fm + '<!-- job:zz25 -->\n## 2025-12-30 — zz25\nbody\n',
            '2026': fm + '<!-- job:zz26 -->\n## 2026-01-05 — zz26\nbody\n'}
    late = '\n<!-- job:late -->\n## 2026-10-12 — late job\n' + body(99)
    want = secs(log + late) + secs(arcs['2025']) + secs(arcs['2026'])
    for n_writes in (1, 2):
        shutil.rmtree(os.path.join(v, 'Projects/demo7'), ignore_errors=True)
        _w(v, 'Projects/demo7/errors.md', log)
        for y, t in arcs.items():
            _w(v, f'Projects/demo7/_archive/errors-{y}.md', t)
        files = [os.path.join(v, 'Projects/demo7', x) for x in
                 ('errors.md', '_archive/errors-2025.md', '_archive/errors-2026.md')]
        _crash_after(n_writes, lambda: rotate(v, apply=True, today=today, what='logs'))
        assert read(files[0]) == log, 'live page written before its archives'
        with open(files[0], 'a', encoding='utf-8') as f:
            f.write(late)
        r = rotate(v, apply=True, today=today, what='logs')[0]
        texts = [read(f) for f in files]
        assert r['verified'] and r['already_archived'] >= 1, r
        assert sum((secs(t) for t in texts), Counter()) == want, ('sections lost or doubled', n_writes)
        assert all(''.join(texts).count(x) == 1 for x in ('hand note A', 'hand note B', 'late job', 'job:zz25'))
    shutil.rmtree(os.path.join(v, 'Projects/demo7'))


def astra3_6b(v, today):
    """Astra round 3 item 6, review variant: a lesson block with no '_(date, session' stamp (hand edit) dates
    from the unit before it. When it is the first block left live (the move stops right after its predecessor,
    or only it remains), the rotated page re-parses it into the header. The no-loss check must count pages, not
    units, or every rotate run aborts with 'would not preserve every section' (it did: dry run and apply)."""
    hdr = '---\ntype: lesson\nproject: P\n---\n\n# P — lessons\n\n**For future Claude:** candidates.\n'

    def block(j, date, stamp, size):
        return '\n'.join([f'<!-- job:{j:020x} -->'] + [
            f'- [candidate] **Rule {j}.{k} ' + 'r' * size + '** — evidence: "e" '
            + (f'_({date}, session {j:08x})_ ' if stamp else '') + f'<!-- id:l-{j:04x}{k:04x} -->' for k in range(4)])

    def page(proj, specs):
        t = hdr.replace('P', proj)
        for j, (stamp, size) in enumerate(specs):
            t = t.rstrip('\n') + '\n\n' + block(j, dt.date(2025, 11, 20) + dt.timedelta(days=3 * j), stamp, size) + '\n'
        return t
    key = lambda t: Counter(ln for ln in t.splitlines() if ln.startswith(('- ', '<!-- job:')))
    _w(v, 'Projects/demo9/lessons.md', page('demo9', [(True, 450)] * 60))
    m = rotate(v, apply=False, today=today, what='logs')[0]['move']  # unstamp the first block left live
    cases = {'demo9': [(j != m, 450) for j in range(60)],
             'demo10': [(True, 10000), (False, 16000)]}  # unstamped newest block is all that stays live
    for proj, specs in cases.items():
        text = page(proj, specs)
        lp = os.path.join(v, 'Projects', proj, 'lessons.md')
        _w(v, f'Projects/{proj}/lessons.md', text)
        dry = rotate(v, apply=False, today=today, what='logs')
        assert len(dry) == 1 and dry[0]['move'] > 0 and read(lp) == text, dry
        _crash_after(1, lambda: rotate(v, apply=True, today=today, what='logs'))  # the interrupted run recovers too
        r = rotate(v, apply=True, today=today, what='logs')[0]
        files = [lp] + [os.path.join(v, a) for a in sorted(r['archives'])]
        texts = [read(f) for f in files]
        unstamped = next(j for j, (st, _) in enumerate(specs) if not st)
        assert r['verified'] and r['move'] == dry[0]['move'] == unstamped and r['already_archived'] >= 1, r
        assert texts[0].split('<!-- job:')[1].startswith(f'{unstamped:020x}'), 'unstamped block must stay first live'
        assert sum((key(t) for t in texts), Counter()) == key(text), 'a lesson or marker was lost or doubled'
        assert sum((secs(t) for t in texts), Counter()) == secs(text)
        assert rotate(v, apply=False, today=today, what='logs') == []
        shutil.rmtree(os.path.join(v, 'Projects', proj))


def self_test():
    global HANDOFF_LOCKS, LOCK_TIMEOUT
    with tempfile.TemporaryDirectory() as v, tempfile.TemporaryDirectory() as locks:
        def w(rel, s):
            os.makedirs(os.path.dirname(os.path.join(v, rel)), exist_ok=True)
            with open(os.path.join(v, rel), 'w', encoding='utf-8') as f:
                f.write(s)
        w('Reference/plain.md', '> compressed note\n\n# Plain page\n\nno frontmatter, links [[ok-target]]\n')            # missing frontmatter
        w('Projects/demo/decisions.md', '---\ntype: decision\n---\n\n# Demo decisions\n[[ghost]] `[[in-code]]`\n')  # no desc + dead link
        w('Actions/old.md', '---\ntype: action\nstatus: active\nupdated: 2020-01-01\ndescription: old\n---\n[[Reference/plain|p]] [[demo]]\n')  # stale
        w('ok-target.md', '---\ntype: note\ndescription: x\n---\n')
        w('_archive/projects/deadp/notes.md', '---\ntype: note\ndescription: x\n---\n')    # archived project, complete
        w('_archive/projects/deadp/plan.md', '# Dead plan\n')                                 # archived project, no frontmatter
        w('harness-memory/m.md', '---\nname: m\ndescription: d\nmetadata:\n  type: feedback\n---\nfact [[slug-n]]\n')
        w('harness-memory/feedback_n.md', '---\nname: slug-n\ndescription: d\nmetadata:\n  type: feedback\n---\nfact\n')
        w('Projects/demo/_archive/decisions-2026.md', '---\ntype: decision\ndescription: d\n---\n' + 'x' * 110000)  # rotated log, size exempt
        w('Projects/demo/_archive/resume-closed.md', '- an item  <!-- id:n-00000000 level:recorded refs: -->\n')  # raw, standard
        today = dt.date(2026, 10, 12)
        hist = '---\ntype: history\n---\n# demo — history\n\n' + ''.join(
            f'## {d} — entry  <!-- job:x -->\n' + 'x' * 40000 + '\n\n## sub\nmore\n\n'
            for d in ('2026-07-01', '2026-08-01', '2026-08-20', '2026-09-01', '2026-09-20', '2026-10-01', '2026-10-10', '2026-10-11'))
        w('Projects/demo/history.md', hist)
        c = report(v, today)['counts']
        exp = {'missing_frontmatter': 2, 'missing_description': 1, 'dead_links': 1, 'stale_active': 1,
               'raw_over_300kb': 1, 'missing_type': 0, 'memory_missing_fields': 0, 'page_over_100kb': 0}
        assert all(c[k] == n for k, n in exp.items()), c
        ch, _, diffs = fill(v, apply=False)
        assert sorted(ch) == ['Projects/demo/decisions.md', 'Reference/plain.md', '_archive/projects/deadp/plan.md'], ch
        fill(v, apply=True)
        fm = parse_fm(split_fm(read(os.path.join(v, 'Reference/plain.md')))[0])
        assert fm['type'] == 'reference' and fm['description'] == 'Plain page' and fm['project'] == 'system', fm
        fm = parse_fm(split_fm(read(os.path.join(v, 'Projects/demo/decisions.md')))[0])
        assert fm['type'] == 'decision' and fm['project'] == 'demo' and fm['description'] == 'Demo decisions', fm
        fm = parse_fm(split_fm(read(os.path.join(v, '_archive/projects/deadp/plan.md')))[0])
        assert fm['project'] == 'deadp' and fm['status'] == 'done', fm
        assert fill(v, apply=False)[0] == []                       # idempotent
        c = report(v, today)['counts']
        assert c['missing_frontmatter'] == 0 and c['missing_description'] == 0, c
        HANDOFF_LOCKS, LOCK_TIMEOUT = locks, 0.3                  # a running distill holds the project lock
        lk = os.path.join(locks, hashlib.sha256(b'project:demo').hexdigest()[:32] + '.lock')
        with open(lk, 'w') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            assert rotate(v, apply=True, today=today)[0].get('skipped') and read(os.path.join(v, 'Projects/demo/history.md')) == hist
        r = rotate(v, apply=True, today=today)                     # lock released: rotates
        assert r[0]['move'] == 4 and r[0]['archive'].endswith('history-2026-07-01-to-2026-09-01.md'), r
        h = read(os.path.join(v, 'Projects/demo/history.md'))
        a = read(os.path.join(v, r[0]['archive']))
        assert h.startswith('---\ntype: history\n---\n# demo — history') and '## 2026-09-20' in h and '2026-09-01' not in h
        assert a.count('## sub') == 4 and '## 2026-07-01' in a and '2026-09-20' not in a
        assert rotate(v, apply=False, today=today) == []           # now under 300 KB
        # append-style log: marker lines, a two-error job, out-of-order dates, an older archive to merge into
        body = lambda n: ('**What:** ' + 'y' * 9000 + f' {n}\n\n')
        log = ('---\ntype: note\nproject: demo\nstatus: active\nupdated: 2026-10-11\ndescription: d\n---\n\n'
               '# demo — errors\n\npreamble\n\n'
               + '## 2026-09-20 — hand note\n' + body(0)
               + '<!-- job:aa -->\n## 2025-12-30 — job aa first\n' + body(1) + '## 2025-12-30 — job aa second\n' + body(2)
               + '### sub\nkept with aa\n\n'
               + '<!-- job:bb -->\n## 2026-01-05 — job bb\n' + body(3)
               + ''.join(f'<!-- job:j{d} -->\n## 2026-10-{d:02d} — job j{d}\n' + body(d + 10) for d in range(1, 11))
               + '## 2026-10-11 — last, no trailing newline\nend')
        w('Projects/demo/errors.md', log)
        w('Projects/demo/_archive/errors-2025.md', '---\ntype: note\nupdated: 2025-12-31\n---\n\n# old\n\n'
          '## 2025-11-01 — older\nold body\n\n## 2026-01-01 — misfiled newer\nx\n')
        lp = os.path.join(v, 'Projects/demo/errors.md')
        hdr, units = split_units(log)
        assert hdr.endswith('preamble\n\n') and [u[0] for u in units][:3] == ['2026-09-20', '2025-12-30', '2026-01-05']
        assert units[1][1].startswith('<!-- job:aa -->') and units[1][1].count('## 2025-12-30') == 2, units[1]
        dry = rotate(v, apply=False, today=today, what='logs')
        assert len(dry) == 1 and dry[0]['move'] == 6 and dry[0]['archives']['Projects/demo/_archive/errors-2025.md'] == 1 and read(lp) == log, dry   # dry-run writes nothing
        r = rotate(v, apply=True, today=today, what='logs')[0]
        assert r['verified'] and r['size_kb_after'] <= 70 and set(r['archives']) == {
            'Projects/demo/_archive/errors-2025.md', 'Projects/demo/_archive/errors-2026.md'}, r
        live, a25, a26 = (read(os.path.join(v, x)) for x in (
            'Projects/demo/errors.md', 'Projects/demo/_archive/errors-2025.md', 'Projects/demo/_archive/errors-2026.md'))
        assert live.startswith(hdr) and 'job:aa' not in live and 'job:bb' not in live and live.endswith('end')
        assert a25.index('2025-11-01') < a25.index('job aa first') < a25.index('job aa second') < a25.index('kept with aa')
        assert 'updated: 2026-10-12' in a25 and a25.index('2026-01-01 — misfiled') > a25.index('kept with aa')
        f26 = parse_fm(split_fm(a26)[0])
        assert f26['type'] == 'note' and f26['project'] == 'demo' and f26['status'] == 'done' and f26['updated'] == '2026-10-12'
        assert a26.index('job:bb') < a26.index('2026-09-20 — hand note'), a26  # date order, not file order
        orig = sorted(_nl(t) for _, t in units) + ['## 2025-11-01 — older\nold body\n\n', '## 2026-01-01 — misfiled newer\nx\n']
        got = sorted(_nl(t) for x in (live, a25, a26) for _, t in split_units(x)[1])
        assert got == sorted(orig), 'union of live + archives must equal the original sections'
        assert rotate(v, apply=False, today=today, what='logs') == []          # idempotent: live is under budget
        assert report(v, today)['counts']['duplicate_basenames'] == 0
        # regroup: the archive ends on a job dated 2026-01-05 and the oldest live section is a marker-less hand
        # note of the same day, so after the merge split_units joins them; that must not read as a loss
        fm2 = '---\ntype: note\nproject: demo2\n---\n\n'
        log2 = (fm2 + '## 2026-01-05 — hand note, same day as the archived job\n' + body(50)
                + ''.join(f'<!-- job:k{d} -->\n## 2026-10-{d:02d} — job k{d}\n' + body(d + 50) for d in range(1, 12)))
        w('Projects/demo2/errors.md', log2)
        w('Projects/demo2/_archive/errors-2026.md', fm2 + '<!-- job:zz -->\n## 2026-01-05 — zz\nbody\n')
        r2 = rotate(v, apply=True, today=today, what='logs')[0]
        a2 = read(os.path.join(v, 'Projects/demo2/_archive/errors-2026.md'))
        assert r2['verified'] and a2.index('job:zz') < a2.index('hand note') and len(split_units(a2)[1]) < r2['move'] + 1, r2
        # crash after the archive write, before the live write: the rerun recovers without a duplicate
        w('Projects/demo3/errors.md', log2.replace('demo2', 'demo3'))
        real, calls = _atomic, []

        def crash(path, text, mode_from=None):
            if path.endswith(os.sep + 'errors.md'):
                raise OSError('simulated crash')
            calls.append(path)
            real(path, text, mode_from)
        globals()['_atomic'] = crash
        try:
            rotate(v, apply=True, today=today, what='logs')
        except OSError:
            pass
        finally:
            globals()['_atomic'] = real
        a3p, l3p = (os.path.join(v, 'Projects/demo3', x) for x in ('_archive/errors-2026.md', 'errors.md'))
        assert calls == [a3p] and read(l3p) == log2.replace('demo2', 'demo3'), calls  # archive written, live untouched
        r3 = rotate(v, apply=True, today=today, what='logs')[0]
        a3 = read(a3p)
        assert r3['verified'] and r3['already_archived'] == r3['move'] and r3['archives'] == {
            'Projects/demo3/_archive/errors-2026.md': 0}, r3
        assert a3.count('hand note') == 1 and 'hand note' not in read(l3p)
        assert sorted(_nl(t) for x in (read(l3p), a3) for _, t in split_units(x)[1]) == sorted(
            _nl(t) for _, t in split_units(log2.replace('demo2', 'demo3'))[1])
        astra3_5(v, today)
        astra3_6(v, today)
        astra3_5b(v, today)
        astra3_6b(v, today)
    print('self-test OK')


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--vault', default=VAULT)
    ap.add_argument('--self-test', action='store_true')
    sub = ap.add_subparsers(dest='cmd')
    sub.add_parser('report').add_argument('--json', action='store_true')
    for name in ('fill', 'rotate'):
        sp = sub.add_parser(name)
        g = sp.add_mutually_exclusive_group(required=True)
        g.add_argument('--dry-run', action='store_true')
        g.add_argument('--apply', action='store_true')
        if name == 'rotate':
            sp.add_argument('--days', type=int, default=30)
            sp.add_argument('--what', choices=('all', 'history', 'logs'), default='all')
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if a.cmd == 'report':
        r = report(a.vault)
        print(json.dumps(r, indent=1, ensure_ascii=False)) if a.json else print_report(r)
    elif a.cmd == 'fill':
        ch, sk, diffs = fill(a.vault, a.apply)
        print(f"fill {'applied' if a.apply else 'dry-run'}: {len(ch)} pages {'changed' if a.apply else 'would change'}"
              f", {len(sk)} skipped (not utf-8)")
        if not a.apply:
            print('\n'.join(diffs))
    elif a.cmd == 'rotate':
        recs = rotate(a.vault, a.apply, a.days, what=a.what)
        print(f"rotate {'applied' if a.apply else 'dry-run'} ({a.what}; history entries older than {a.days} days, "
              f"logs over {LOG_MAX // KB} KB down to {LOG_TARGET // KB} KB):")
        for rec in recs:
            print('  ' + json.dumps(rec, ensure_ascii=False))
        if not recs:
            print('  nothing over budget')
    else:
        ap.print_help()


if __name__ == '__main__':
    sys.exit(main())
