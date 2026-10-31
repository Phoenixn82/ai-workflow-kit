"""Deterministic injection signals with offsets into normalized source text."""

import base64
from bisect import bisect_left, bisect_right
import binascii
import codecs
from dataclasses import dataclass
import html
import re
from typing import Literal
import unicodedata


@dataclass(frozen=True)
class Hit:
    rule: str
    offset: int
    length: int


RULES: dict[str, str] = {
    'T1': 'override instructions',
    'T2': 'role and turn markers',
    'T3': 'text addressed to AI',
    'T4': 'tool and shell syntax',
    'T5': 'exfiltration asks',
    'T6': 'output steering',
    'T7': 'forged fences',
    'T8': 'hidden channels',
    'T9': 'encoded instructions',
    'T10': 'other language overrides',
    'T11': 'platform bait',
}

_OVERRIDE = (r'\b(?:ignore|disregard|forget|override|bypass)\W+(?:\w+\W+){0,4}'
             r'(?:previous|prior|above|earlier|all|your)\W+(?:\w+\W+){0,3}'
             r'(?:instructions?|prompts?|rules|directives|guidelines|context)\b')
_SESSION_OVERRIDE = (r'\b(?:ignore|disregard|forget|override)\s+'
                     r'(?:(?:all(?:\s+the)?|the)\s+)?'
                     r'(?:previous|prior|above|earlier|preceding|original|initial|your|system)'
                     r'\s+(?:(?:system|previous|prior)\s+)?'
                     r'(?:instructions?|prompts?|rules|directions|guidelines|context)\b')
_ROLES = (r'^[ \t]*(?:system|assistant|developer|human):|'
          r'<\|(?:im_start|im_end|system|assistant|user|start_header_id|end_header_id|eot_id|'
          r'start|end|message|endoftext)\|>|\[/?(?:inst|system)\]|<<sys>>|'
          r'<system(?:-reminder)?\b[^>]*>|\bbegin system prompt\b|^### instructions?\b')
_TOOLS = r'<function_calls\b[^>]*>|<invoke\b|"tool_use"'
# Session markers must begin a line, after optional quote or list prefixes.
_MARKER_START = r'^[ \t]*(?:(?:[>"\']|[-*+] |\d+[.)] )[ \t]*)*'
_SESSION_ROLES = (_MARKER_START +
                  r'(?:system:|assistant:|developer:|human:|'
                  r'<\|(?:im_start|im_end|system|assistant|user|start_header_id|end_header_id|'
                  r'eot_id|start|end|message|endoftext)\|>|\[/?(?:inst|system)\]|<<sys>>|'
                  r'<system(?:-reminder)?>|begin system prompt(?:[ \t]*$|:)|'
                  r'### (?:instruction|system)(?:[ \t]*$|:))')
_SESSION_TOOLS = (_MARKER_START + r'(?:<(?:pre|code)(?:[ \t][^>]*)?>[ \t]*)*' +
                  r'(?:<function_calls>|<invoke(?=[ \t>])|'
                  r'(?:\{[ \t]*)?"type"[ \t]*:[ \t]*"tool_use"|"tool_use"[ \t]*:)')
_EXFIL_START = re.compile(
    r'(?:^|(?<=[.!?])[ \t]+)[ \t]*(?:[-*+] |\d+[.)] )?'
    r'(?:(?:please|now|then|also)[ \t]+)?'
    r'(?:send|post|upload|forward|exfiltrate|transmit|paste|email|push)\b', re.MULTILINE)
_SENTENCE_END = re.compile(r'[.!?](?=[ \t\n]|$)|\n')
_SENSITIVE = re.compile(
    r'\b(?:credentials?|api keys?|tokens?|passwords?|cookies?|secrets?|'
    r'environment variables|ssh (?:private )?keys?|private keys?|system prompts?|'
    r'(?:conversation|chat) history|memory|file contents|everything)\b|'
    r'(?<!\w)\.env\b|'
    r'\b(?:extracted|scraped|collected|harvested|all|every|the whole|the entire|the full)'
    r'[ \t]+(?:\w+[ \t]+){0,3}'
    r'(?:rows|records|results|data|dataset|leads|contacts|list|output|files?|conversation|history)\b')
_EXTERNAL = re.compile(r'\b[a-z][a-z0-9+.-]*://[^\s<>"\']+|'
                       r'(?<![\w.+-])[\w.+-]+@[\w.-]+\.[a-z]{2,}\b')


_MULTILINGUAL = (
    r'\b(?:ignora|ignore|ignorar)\b.{0,40}\b(?:instrucciones|instruções)\b|'
    r'\b(?:ignorez|ignorer)\b.{0,40}\b(?:instructions|consignes)\b|'
    r'\bignoriere\b.{0,40}\banweisungen\b|'
    r'\b(?:ignora|ignorare)\b.{0,40}\bistruzioni\b|'
    r'忽略(?:之前|以上|先前|所有)|'
    r'(?:以前|前|これまで|すべて|全て)の.{0,20}(?:指示|命令).{0,10}無視|'
    r'(?:이전|앞의|모든).{0,20}(?:지시|명령).{0,10}무시|'
    r'\bигнорируй\b|\bигнорируйте\b|'
    r'\bbỏ qua\b.{0,40}\bhướng dẫn\b'
)

# Session overrides name instructions and an existing-instruction qualifier.
_SESSION_MULTILINGUAL = (
    r'\b(?:ignora|ignore|ignorar)\s+(?:(?:todas?|todas? las|todas? as|las|as)\s+)?'
    r'(?:instrucciones|instruções)\s+(?:anteriores|previas|originais|originales)\b|'
    r'\b(?:ignorez|ignorer)\s+(?:(?:toutes les|les)\s+)?'
    r'(?:instructions|consignes)\s+(?:précédentes|antérieures|originales)\b|'
    r'\bignoriere\s+(?:alle\s+)?(?:vorherigen|bisherigen|ursprünglichen)\s+anweisungen\b|'
    r'\b(?:ignora|ignorare)\s+(?:(?:tutte le|le)\s+)?'
    r'istruzioni\s+(?:precedenti|originali)\b|'
    r'忽略(?:之前|以上|先前|原始|系统|你的|所有)(?:的)?(?:所有)?(?:指令|指示|命令|规则|提示)|'
    r'(?:以前|前|これまで|すべて|全て)の(?:システム)?(?:指示|命令)を?無視|'
    r'(?:이전|앞의|모든)\s+(?:시스템\s+)?(?:지시|명령)(?:를|을)?\s*무시|'
    r'\bигнориру(?:й|йте)\s+(?:все\s+)?(?:предыдущие|прежние|исходные|системные|ваши)'
    r'\s+(?:инструкции|указания|правила)\b|'
    r'\bbỏ qua\s+(?:(?:tất cả các|các)\s+)?hướng dẫn\s+(?:trước đó|trước đây)\b'
)

_PIPELINE = {
    'T1': _OVERRIDE,
    'T2': _ROLES,
    'T3': (r'\b(?:note|message|instructions?|notice)\s+(?:to|for)\s+(?:any\s+)?'
           r'(?:ai|llm|language models?|assistants?|agents?|scrapers?|crawlers?|bots?|'
           r'data (?:collectors?|compilers?))\b|'
           r'\b(?:ai|assistant|agent|llm|gpt|claude|codex|gemini)s?\b.{0,40}'
           r'\b(?:must|should|are required|are authorized|reading this)\b'),
    'T4': (_TOOLS + r'|"tool"\s*:|"function_call"|'
           r'\b(?:curl|wget|bash|sh -c|powershell|rm -rf|scp|ssh)\s|base64 -d|\$\('),
    'T5': (r'\b(?:print|reveal|repeat|output|include|send|share|post|paste)\W+'
           r'(?:\w+\W+){0,6}(?:system prompt|your instructions|environment variables|'
           r'api keys?|tokens?|credentials?|ssh keys?|\.env|id_ed25519|\.credentials\.json)\b'),
    'T6': (r'\b(?:set|change|replace|report|record|list|mark)\W+(?:\w+\W+){0,4}'
           r'(?:price|email|phone|website|contact|condition|status)\W+(?:\w+\W+){0,2}'
           r'(?:to|as)\b|\badd\s+(?:these|the following)\s+(?:\d+\s+)?'
           r'(?:rows|companies|entries|leads|suppliers)\b'),
    'T7': r'end[ _-]?(?:web[ _-]?content|untrusted|page)|untrusted_web_data',
    'T10': _MULTILINGUAL,
    'T11': (r'\bfiverr (?:trust|support|team)\b|'
            r'\b(?:contact|email|whatsapp|telegram|skype) (?:me|us) (?:directly|outside|off)\b|'
            r'\bskip fiverr\b|\bsign in (?:with google )?to (?:keep|verify)\b'),
}
_SESSION = {'T1': _SESSION_OVERRIDE, 'T2': _SESSION_ROLES, 'T4': _SESSION_TOOLS,
            'T10': _SESSION_MULTILINGUAL}
_PATTERNS = {profile: {rule: re.compile(pattern, re.MULTILINE)
                      for rule, pattern in patterns.items()}
             for profile, patterns in [('pipeline', _PIPELINE), ('session', _SESSION)]}
_TAGS = re.compile('[\U000e0000-\U000e007f]+')
_BIDI = re.compile('[\u202a-\u202e\u2066-\u2069]')
_ZERO_WIDTH = re.compile('[\u200b-\u200d\ufeff]')
_BASE64 = re.compile(r'(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{24,}={0,2}(?![A-Za-z0-9+/=])')
_PERCENT = re.compile(r'(?:%[0-9a-f]{2})+')
_ENTITIES = re.compile(r'&(?:#[0-9]+;?|#[xX][0-9a-fA-F]+;?|[^\t\n\f <&#;]{1,32};?)')


def normalize(text: str) -> str:
    """Decode HTML and Unicode tags before stripping format characters."""
    text = html.unescape(text)
    text = ''.join(chr(ord(c) - 0xE0000) if 0xE0020 <= ord(c) <= 0xE007E else c
                   for c in text)
    text = unicodedata.normalize('NFKC', text).casefold()
    text = ''.join(c for c in text if unicodedata.category(c) != 'Cf')
    return re.sub('[ \t]+', ' ', text.replace('\r\n', '\n').replace('\r', '\n'))


def _short_quote_ends(text: str) -> dict[int, int]:
    """Pair delimiters once; only short complete spans suppress instructions."""
    opening = {'"': '"', "'": "'", '`': '`', '“': '”', '‘': '’'}
    active, ends = {}, {}
    escaped = False
    for index, char in enumerate(text):
        if escaped:
            escaped = False
            continue
        if char == '\\':
            escaped = True
            continue
        if (char in "'’" and index and index + 1 < len(text)
                and text[index - 1].isalnum() and text[index + 1].isalnum()):
            continue  # Apostrophes within words are not quote delimiters.
        if char in active:
            left = active.pop(char)
            if index + 1 - left <= 80:
                for position in range(left + 1, index):
                    ends[position] = max(ends.get(position, 0), index + 1)
        elif char in opening:
            active[opening[char]] = index
    return ends


def _matches(text: str, profile: str, platform: bool = False, decoded: bool = False) -> list[Hit]:
    hits = []
    for rule, pattern in _PATTERNS[profile].items():
        if (rule == 'T11' and (not platform or decoded)
                or decoded and rule not in {'T1', 'T2', 'T3', 'T4', 'T5', 'T6'}):
            continue
        quote_ends = None
        for match in pattern.finditer(text):
            if profile == 'session' and rule == 'T1':
                if quote_ends is None:
                    quote_ends = _short_quote_ends(text)
                if quote_ends.get(match.start(), 0) > match.end():
                    continue
            hits.append(Hit(rule, match.start(), match.end() - match.start()))
    if profile == 'session':
        for match in _EXFIL_START.finditer(text):
            # Punctuation inside URLs, addresses and .env is not a sentence break.
            end = _SENTENCE_END.search(text, match.end())
            stop = end.start() if end else len(text)
            sentence = text[match.start():stop].strip()
            if (len(sentence) <= 200 and _SENSITIVE.search(_EXTERNAL.sub('', sentence))
                    and _EXTERNAL.search(sentence)):
                hits.append(Hit('T5', match.start(), stop - match.start()))
    return hits


def _normalized_offsets(source: str) -> tuple[str, list[int]]:
    """Map unescaped source boundaries in one pass over normalization units.

    Combining marks and composing starters stay in NFKC-stable segments.
    Boundaries inside a composed unit map to its end; signals start at starters.
    """
    offsets = [0] * (len(source) + 1)
    output = []
    unit = []
    unit_start = 0
    previous_cr = False

    def flush(end):
        nonlocal previous_cr
        value = unicodedata.normalize('NFKC', ''.join(unit)).casefold()
        for char in value:
            if unicodedata.category(char) == 'Cf':
                continue
            if char == '\n' and previous_cr:
                previous_cr = False
                continue
            previous_cr = char == '\r'
            char = '\n' if previous_cr else ' ' if char in ' \t' else char
            if char != ' ' or not output or output[-1] != ' ':
                output.append(char)
        for index in range(unit_start + 1, end + 1):
            offsets[index] = len(output)

    for index, char in enumerate(source):
        if 0xE0020 <= ord(char) <= 0xE007E:
            char = chr(ord(char) - 0xE0000)
        decomposed = unicodedata.normalize('NFKD', char)
        if unit and not unicodedata.combining(decomposed[0]):
            # Class-zero starters can compose too (Indic vowels and Hangul).
            # Probe only at starter boundaries, never for each combining mark.
            # Composing starter chains are bounded by Unicode decompositions;
            # a long mark run is probed once, keeping total work linear.
            left = unicodedata.normalize('NFKC', ''.join(unit))
            right = unicodedata.normalize('NFKC', char)
            if unicodedata.normalize('NFKC', left + right) == left + right:
                flush(index)
                unit = []
                unit_start = index
        unit.append(char)
    flush(len(source))
    return ''.join(output), offsets


def _source_hit(rule: str, offsets: list[int], start: int, end: int, size: int) -> Hit:
    # Encoded signals point at the encoded source span, never at decoded-only text.
    offset, stop = offsets[start], offsets[end]
    if stop == offset and size:
        offset = min(offset, size - 1)
        stop = offset + 1
    return Hit(rule, offset, stop - offset)


def _percent_hits(text: str, profile: str) -> list[Hit]:
    """Decode across literal whitespace, keeping each decoded character's source span."""
    if not _PERCENT.search(text):
        return []
    decoded, starts, ends = [], [], []
    cursor = 0
    for match in _PERCENT.finditer(text):
        decoded.extend(text[cursor:match.start()])
        starts.extend(range(cursor, match.start()))
        ends.extend(range(cursor + 1, match.start() + 1))
        decoder = codecs.getincrementaldecoder('utf-8')('replace')
        pending = match.start()
        for index in range(match.start(), match.end(), 3):
            value = decoder.decode(bytes([int(text[index + 1:index + 3], 16)]),
                                   final=index + 3 == match.end())
            decoded.extend(value)
            starts.extend([pending] * len(value))
            ends.extend([index + 3] * len(value))
            if value:
                pending = index + 3
        cursor = match.end()
    decoded.extend(text[cursor:])
    starts.extend(range(cursor, len(text)))
    ends.extend(range(cursor + 1, len(text) + 1))
    # Preserve the HTML decoding that normalize applies to encoded payloads.
    value = ''.join(decoded)
    pieces, unescaped_starts, unescaped_ends = [], [], []
    cursor = 0
    for match in _ENTITIES.finditer(value):
        pieces.append(value[cursor:match.start()])
        unescaped_starts.extend(starts[cursor:match.start()])
        unescaped_ends.extend(ends[cursor:match.start()])
        replacement = html.unescape(match[0])
        pieces.append(replacement)
        unescaped_starts.extend([starts[match.start()]] * len(replacement))
        unescaped_ends.extend([ends[match.end() - 1]] * len(replacement))
        cursor = match.end()
    pieces.append(value[cursor:])
    starts = unescaped_starts + starts[cursor:]
    ends = unescaped_ends + ends[cursor:]
    normalized, offsets = _normalized_offsets(''.join(pieces))
    hits = []
    for hit in _matches(normalized, profile, decoded=True):
        start = bisect_right(offsets, hit.offset) - 1
        end = bisect_left(offsets, hit.offset + hit.length)
        left, right = starts[start], ends[end - 1]
        if _PERCENT.search(text, left, right):
            hits.append(Hit('T9', left, right - left))
    return hits


def scan(text: str, profile: Literal['pipeline', 'session'] = 'pipeline',
         platform: bool = False) -> list[Hit]:
    """Return fixed rule IDs and normalized offsets, without storing excerpts."""
    if profile not in _PATTERNS:
        raise ValueError('unknown tripwire profile')
    normalized = normalize(text)
    hits = _matches(normalized, profile, platform)
    raw = html.unescape(text)
    offsets = None

    def source_hit(rule, start, end):
        nonlocal offsets
        if offsets is None:
            _, offsets = _normalized_offsets(raw)
        return _source_hit(rule, offsets, start, end, len(normalized))

    for match in _TAGS.finditer(raw):
        if profile == 'pipeline' or len(match[0]) >= 3:
            hits.append(source_hit('T8', match.start(), match.end()))
    bidi = list(_BIDI.finditer(raw))
    if profile == 'pipeline':
        for match in bidi:
            hits.append(source_hit('T8', match.start(), match.end()))
        zero = list(_ZERO_WIDTH.finditer(raw))
        for first, third in zip(zero, zero[2:]):
            if third.start() - first.start() < 20:
                hits.append(source_hit('T8', first.start(), third.end()))
    else:
        # A pair within 200 characters with 12 payload characters avoids short RTL labels.
        for first, second in zip(bidi, bidi[1:]):
            if (second.start() - first.start() <= 200
                    and len(normalize(raw[first.end():second.start()]).strip()) >= 12):
                hits.append(source_hit('T8', first.start(), second.end()))

    for match in _BASE64.finditer(raw):
        try:
            value = base64.b64decode(match[0] + '=' * (-len(match[0]) % 4), validate=True)
        except (ValueError, binascii.Error):
            continue
        if value and sum(32 <= c <= 126 or c in (9, 10, 13) for c in value) / len(value) >= .7:
            if _matches(normalize(value.decode('ascii', errors='replace')), profile, decoded=True):
                hits.append(source_hit('T9', match.start(), match.end()))
    hits.extend(_percent_hits(normalized, profile))
    for hit in _matches(codecs.decode(normalized, 'rot_13'), profile, decoded=True):
        hits.append(Hit('T9', hit.offset, hit.length))
    return sorted(set(hits), key=lambda hit: (hit.offset, hit.rule, hit.length))
