#!/usr/bin/env python3
"""Install the marker-delimited Hermes harness; --self-test uses only temporary homes."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

import yaml

HERE = Path(__file__).resolve().parent
START, END = '# >>> ai-workflow-kit hermes >>>', '# <<< ai-workflow-kit hermes <<<'
INSTALL = ('curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash -s -- '
           '--non-interactive --skip-browser --skip-computer-use')
KEYS = ('providers', 'fallback_providers', 'model_aliases', 'auxiliary', 'mcp_servers', 'timeouts')


def hosts_env(key):
    if os.environ.get(key):
        return os.environ[key]
    path = Path(os.environ.get('KIT_HOSTS_ENV') or Path.home() / '.config/ai-workflow-kit/hosts.env')
    vals = [v.strip().strip("\"'") for k, _, v in
            (line.partition('=') for line in path.read_text().splitlines()) if k.strip() == key] if path.exists() else []
    return vals[-1] if vals else ''


def replace_block(text, block, start=START, end=END):
    if text.count(start) != text.count(end) or text.count(start) > 1:
        raise ValueError('malformed harness markers')
    if start in text:
        a, b = text.index(start), text.index(end) + len(end)
        if b < a:
            raise ValueError('reversed harness markers')
        return text[:a] + block.rstrip('\n') + text[b:]
    return text + ('\n' if text and not text.endswith('\n') else '') + block


class UniqueLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node):
    result = {}
    for key, value in node.value:
        k = loader.construct_object(key)
        if k in result:
            raise ValueError(f'duplicate YAML key: {k}')
        result[k] = loader.construct_object(value)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def validate(text):
    data = yaml.load(text, Loader=UniqueLoader)
    if data is not None and not isinstance(data, dict):
        raise ValueError('config must be a YAML mapping')
    return data or {}


def backup(path, stamp):
    if path.exists() or path.is_symlink():
        shutil.copy2(path, str(path) + '.bak-' + stamp, follow_symlinks=True)


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as f:
        f.write(text)
    os.replace(f.name, path)


def install(a):
    if not shutil.which('hermes'):
        print('Install Hermes first: ' + INSTALL)
        return 3
    router = hosts_env('FREELLMAPI_URL').rstrip('/')
    if not router:
        print('set FREELLMAPI_URL (the /v1 URL) in hosts.env')
        return 2
    if a.wake_proxy and not hosts_env('THOR_HOST'):
        print('set THOR_HOST in hosts.env')
        return 2
    home = Path.home()
    os.environ['HERMES_HOME'] = str(home / '.hermes')
    config, soul = home / '.hermes/config.yaml', home / '.hermes/SOUL.md'
    old = config.read_text() if config.exists() else ''
    outside = replace_block(old, '')
    existing = validate(outside)
    collisions = [k for k in KEYS if k in existing]
    if collisions:
        print('colliding top-level keys: ' + ', '.join(collisions))
        return 4
    ollama = hosts_env('THOR_OLLAMA_URL').rstrip('/')
    local = bool(ollama) and not a.no_local
    if ollama and not ollama.endswith('/v1'):
        ollama += '/v1'
    if a.wake_proxy:
        router, ollama = 'http://127.0.0.1:3002/v1', 'http://127.0.0.1:11435/v1'
    key = home / '.config/ai-workflow-kit/freellmapi.key'
    if not key.exists():
        print('Create the router key without printing it:')
        print('  mkdir -p ~/.config/ai-workflow-kit; (umask 077; read -rs -p "Router key: " key; printf "%s" "$key" > ~/.config/ai-workflow-kit/freellmapi.key; unset key)')
    elif key.stat().st_mode & 0o077:
        print('router key must be mode 0600: chmod 600 ~/.config/ai-workflow-kit/freellmapi.key')
        return 2
    values = {'ROUTER_URL': router, 'OLLAMA_URL': ollama,
              'KEY_CMD': 'cat ' + shlex.quote(str(key)),
              'LOCAL_MODEL': os.environ.get('HERMES_LOCAL_MODEL') or 'qwen3.6-gpu:35b',
              'HELPER_WALL': str(home / '.local/bin/helper-wall')}
    block = (HERE / 'config-fleet.yaml').read_text()
    # Substitute YAML string contents, including quotes and unusual home paths.
    for name, value in values.items():
        block = block.replace('@' + name + '@', json.dumps(value)[1:-1])
    fleet = validate(block)
    if not local:
        del fleet['providers']['thor']
        fleet['fallback_providers'] = [f for f in fleet['fallback_providers'] if f['provider'] != 'custom:thor']
        del fleet['model_aliases']['local']
        del fleet['auxiliary']['vision']
        block = START + '\n' + yaml.safe_dump(fleet, sort_keys=False) + END + '\n'
    new = replace_block(old, block)
    validate(new)
    soul_start, soul_end = '<!-- >>> ai-workflow-kit hermes >>> -->', '<!-- <<< ai-workflow-kit hermes <<< -->'
    new_soul = replace_block(soul.read_text() if soul.exists() else '', (HERE / 'SOUL-addendum.md').read_text(),
                             soul_start, soul_end)
    if a.dry_run:
        print('Would back up config.yaml and SOUL.md, replace harness blocks, set model/skills, disable web/browser,')
        print('link helper-wall and aihermes' + (', install and start the wake proxy' if a.wake_proxy else '') +
              ', and install the Herdr integration if available. No files changed.')
        return 0
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup(config, stamp)
    backup(soul, stamp)
    write(config, new)
    write(soul, new_soul)
    for key_name, value in [('model.default', 'auto:code-biz'), ('model.provider', 'custom:freellm')]:
        subprocess.run(['hermes', 'config', 'set', key_name, value], check=True)
    if (home / '.claude/skills').is_dir():
        dirs = validate(config.read_text()).get('skills', {}).get('external_dirs', [])
        dirs = list(dict.fromkeys([*dirs, '~/.claude/skills']))
        subprocess.run(['hermes', 'config', 'set', 'skills.external_dirs', json.dumps(dirs)], check=True)
    bindir = home / '.local/bin'
    bindir.mkdir(parents=True, exist_ok=True)
    links = [(HERE.parent / 'helper-wall/helper-wall', bindir / 'helper-wall'),
             (HERE / 'aihermes', bindir / 'aihermes')]
    if a.wake_proxy:
        links += [(HERE.parent / 'thor-wake-proxy/thor-wake-proxy', bindir / 'thor-wake-proxy'),
                  (HERE.parent / 'thor-wake-proxy/thor-wake-proxy.service',
                   home / '.config/systemd/user/thor-wake-proxy.service')]
    for src, dest in links:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.is_symlink() and dest.resolve() == src.resolve():
            continue
        backup(dest, stamp)
        if dest.exists() or dest.is_symlink():
            dest.unlink()
        dest.symlink_to(src)
    subprocess.run(['hermes', 'tools', 'disable', 'web', 'browser'], check=True)
    if a.wake_proxy:
        subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
        subprocess.run(['systemctl', '--user', 'enable', '--now', 'thor-wake-proxy'], check=True)
    if shutil.which('herdr'):
        subprocess.run(['herdr', 'integration', 'install', 'hermes'], check=True)
    # Hermes commands may reserialize YAML; restore markers after all config-writing commands.
    after = validate(config.read_text())
    for k in KEYS:
        after.pop(k, None)
    final = yaml.safe_dump(after, sort_keys=False) if after else ''
    final = replace_block(final, block)
    validate(final)
    write(config, final)
    print('Hermes harness installed. Run aihermes in a new session.')
    return 0


def self_test():
    with tempfile.TemporaryDirectory() as td:
        h = Path(td)
        b = h / 'bin'
        b.mkdir()
        stub = b / 'hermes'
        stub.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys, yaml
h = pathlib.Path.home()
with (h / 'calls').open('a') as f: f.write(json.dumps(sys.argv[1:]) + '\\n')
if sys.argv[1:3] == ['config', 'set']:
 p = h / '.hermes/config.yaml'; x = yaml.safe_load(p.read_text()) or {}
 key, value = sys.argv[3:5]; parts = key.split('.'); dest = x
 for k in parts[:-1]: dest = dest.setdefault(k, {})
 dest[parts[-1]] = yaml.safe_load(value)
 p.write_text(yaml.safe_dump(x, sort_keys=False))
if sys.argv[1:3] == ['tools', 'disable']:
 p = h / '.hermes/config.yaml'; x = yaml.safe_load(p.read_text()) or {}
 x['test_disabled_tools'] = sys.argv[3:]
 p.write_text(yaml.safe_dump(x, sort_keys=False))
''')
        stub.chmod(0o755)
        for name in ['systemctl', 'herdr']:
            p = b / name
            p.write_text('#!/bin/sh\nexit 0\n')
            p.chmod(0o755)
        env = dict(os.environ, HOME=td, PATH=str(b) + ':' + os.environ['PATH'], HERMES_HOME=str(h / '.hermes'),
                   KIT_HOSTS_ENV=str(h / 'hosts.env'))
        for k in ['FREELLMAPI_URL', 'THOR_HOST', 'THOR_OLLAMA_URL']:
            env.pop(k, None)
        (h / 'hosts.env').write_text('FREELLMAPI_URL=https://router.example/v1\nTHOR_OLLAMA_URL=http://thor:11434\nTHOR_HOST=thor\n')
        (h / '.claude/skills').mkdir(parents=True)
        def run(*args, rc=0):
            p = subprocess.run([sys.executable, str(HERE / 'install.py'), *args], env=env, capture_output=True, text=True)
            assert p.returncode == rc, (args, p.returncode, p.stdout, p.stderr)
            return p.stdout
        run('--dry-run')
        assert not (h / '.hermes').exists()
        run()
        cfg = h / '.hermes/config.yaml'
        first = cfg.read_bytes()
        soul = (h / '.hermes/SOUL.md').read_bytes()
        run()
        assert cfg.read_bytes() == first and (h / '.hermes/SOUL.md').read_bytes() == soul, 'not idempotent'
        assert cfg.read_text().count(START) == 1
        fleet = validate(cfg.read_text())
        assert fleet['model']['default'] == 'auto:code-biz'
        assert all(m['context_length'] == 65536 for p in fleet['providers'].values() for m in p['models'].values())
        assert all(job['model'].endswith('-biz') for name, job in fleet['auxiliary'].items() if name != 'vision')
        assert fleet['timeouts']['tools'] == {'sequential_call': 1800, 'concurrent_batch': 1800}
        assert ['tools', 'disable', 'web', 'browser'] in [json.loads(l) for l in (h / 'calls').read_text().splitlines()]
        cfg.write_text(cfg.read_text().replace('timeout: 1800', 'timeout: 999'))
        run()
        assert 'timeout: 999' not in cfg.read_text()
        cfg.write_text('providers: {}\nmcp_servers: {}\n')
        before = cfg.read_bytes()
        out = run(rc=4)
        assert 'providers, mcp_servers' in out and cfg.read_bytes() == before
        cfg.write_text('providers: {}\nproviders: {}\n')
        duplicate = cfg.read_bytes()
        run(rc=1)
        assert cfg.read_bytes() == duplicate
        cfg.write_bytes(first)
        env['FREELLMAPI_URL'] = ''
        (h / 'hosts.env').write_text('FREELLMAPI_URL=\n')
        run(rc=2)
        assert cfg.read_bytes() == first
        (h / 'hosts.env').write_text('FREELLMAPI_URL=https://router.example/v1\nTHOR_HOST=thor\n')
        run('--no-local', '--wake-proxy')
        data = validate(cfg.read_text())
        assert 'thor' not in data['providers'] and 'local' not in data['model_aliases']
        assert data['providers']['freellm']['api'] == 'http://127.0.0.1:3002/v1'
        assert (h / '.config/systemd/user/thor-wake-proxy.service').is_symlink()
        assert list((h / '.hermes').glob('config.yaml.bak-*'))
    print('hermes-kit self-test: ok (fresh, markers, collisions, missing URL, idempotence, dry run, no-local, proxy)')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for flag in ['dry-run', 'self-test', 'wake-proxy', 'no-local']:
        p.add_argument('--' + flag, action='store_true')
    a = p.parse_args()
    if a.self_test:
        self_test()
        return 0
    return install(a)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, yaml.YAMLError, OSError, subprocess.CalledProcessError) as e:
        print(f'hermes-kit: {e}', file=sys.stderr)
        sys.exit(1)
