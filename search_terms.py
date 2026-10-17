"""Search punctuation is spacing; edition/character words remain intact."""
import re
import unicodedata


def search_query(value):
    value = unicodedata.normalize('NFKC', value or '')
    value = ''.join(' ' if unicodedata.category(char) in ('Pd', 'Ps', 'Pe', 'Pi', 'Pf')
                    or char in '~〜～−・:：/\\\"\'' else char for char in value)
    return re.sub(r'\s+', ' ', value).strip()[:100]


def shorter_query(value):
    value = search_query(value)
    shorter = re.sub(r'(?i)\b(?:BANDAI SPIRITS|BANPRESTO|FIGURE)\b', ' ', value)
    shorter = shorter.replace('バンプレスト', ' ').replace('フィギュア', ' ')
    shorter = re.sub(r'\s+', ' ', shorter).strip()
    return shorter if len(shorter.split()) >= 2 else value
