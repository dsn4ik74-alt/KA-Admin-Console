"""Display only platform requirements explicitly published for this release."""
import re

from its_portal import Document

PLATFORM = re.compile(r'(?<![\d.])8\.\d+\.\d+\.\d+(?!\d|\.\d)')


def table_minimums(document):
    """Map rows to their named column, without confusing update/config versions."""
    result = {}
    for table in document.walk('table'):
        column = None
        for row in table.walk('tr'):
            if any(child is not row for child in row.walk('tr')):
                continue
            cells = [n for n in row.children if getattr(n, 'tag', '') in ('th', 'td')]
            for index, cell in enumerate(cells):
                text = cell.text().casefold()
                if 'минимальн' in text and 'платформ' in text:
                    column = index
            if column is not None and column < len(cells):
                value = cells[column].text()
                if PLATFORM.search(value):
                    result[id(row)] = value
    return result


def requirements(source, url, minimum=''):
    document = Document(source).root
    candidates = []
    for node in document.walk():
        if node.tag not in ('p', 'div', 'li', 'td', 'section'):
            continue
        text = node.text()
        lower = text.casefold()
        if not PLATFORM.search(text) or not ('платформ' in lower or '1с:предприятие' in lower):
            continue
        if not re.search(r'не\s+(?:ниже|менее)|минимальн|рекоменд|предназначен|требует|поддержива|использован', lower):
            continue
        candidates.append(text)
    # A portal layout can nest the same notice in many divs. Keep its smallest
    # block, including exclusions and branch-specific restrictions verbatim.
    blocks = []
    for text in sorted(set(candidates), key=len):
        if not any(block in text for block in blocks):
            blocks.append(text)
    excerpts = []
    for block in blocks:
        for sentence in re.split(r'(?<=[.!?;])\s+(?=[А-ЯЁA-Z])', block):
            lower = sentence.casefold()
            if PLATFORM.search(sentence) and ('платформ' in lower or '1с:предприятие' in lower or 'рекоменд' in lower):
                if sentence not in excerpts:
                    excerpts.append(sentence[:2200])
    text = '\n'.join(excerpts[:8])
    declared = re.findall(r'не\s+(?:ниже|менее)\s+(' + PLATFORM.pattern + ')', text, re.IGNORECASE)
    table_minimum = minimum
    if declared:
        minimum = ', '.join(dict.fromkeys(declared))
    recommended = re.findall(r'рекоменд\w*\s*(?:верси[яи]\s*)?[:—–-]?\s*(' + PLATFORM.pattern + ')', text, re.IGNORECASE)
    return dict(minimum=minimum, table_minimum=table_minimum, recommended=', '.join(dict.fromkeys(recommended)), text=text,
                source=url, known=bool(minimum or text))


def requirements_text(value):
    value = value or {}
    lines = ['Платформа 1С · требования выбранного релиза']
    lines.append('Минимальная: ' + (value.get('minimum') or 'не указана в карточке релиза'))
    lines.append('Рекомендуемая: ' + (value.get('recommended') or 'не указана в карточке релиза'))
    if value.get('table_minimum') and value['table_minimum'] != value.get('minimum'):
        lines.append('В таблице релизов указано: ' + value['table_minimum'] + '. Выше приведено требование карточки релиза.')
    if value.get('text'):
        lines.append(value['text'])
    if value.get('source'):
        lines.append('Источник: ' + value['source'])
    return '\n'.join(lines)
