"""Перевірка лендінгів: відтворює текст кожною мовою і шукає брак."""
import re, sys
from html.parser import HTMLParser

CYR = re.compile(r'[а-яА-ЯіїєґІЇЄҐёЁ]')
POL = re.compile(r'[ąćęłńśźżĄĆĘŁŃŚŹŻ]')
LANGS = ("uk", "pl", "en")

class LangExtractor(HTMLParser):
    """Витягує текст так, як його побачить читач обраною мовою."""
    def __init__(self, want):
        super().__init__(convert_charrefs=True)
        self.want = want
        self.out = []
        self.stack = []      # стек класів мови
        self.skip_depth = 0
        self.in_style_or_script = False

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script"):
            self.in_style_or_script = True
            return
        cls = dict(attrs).get("class", "")
        langs = [l for l in LANGS if l in cls.split()]
        if self.skip_depth:
            self.skip_depth += 1
            self.stack.append(None)
            return
        if langs and langs[0] != self.want:
            self.skip_depth = 1
            self.stack.append(None)
        else:
            self.stack.append(langs[0] if langs else None)

    def handle_endtag(self, tag):
        if tag in ("style", "script"):
            self.in_style_or_script = False
            return
        if tag in ("br", "img", "link", "meta", "hr"):
            return
        if self.stack:
            self.stack.pop()
        if self.skip_depth:
            self.skip_depth -= 1

    def handle_data(self, data):
        if self.in_style_or_script or self.skip_depth:
            return
        if data.strip():
            self.out.append(" ".join(data.split()))

    def text(self):
        return "\n".join(self.out)

def check(path):
    html = open(path, encoding="utf-8").read()
    print("=" * 72)
    print(f"  {path}")
    print("=" * 72)

    counts = {l: html.count(f'class="{l}"') for l in LANGS}
    print(f"блоків: uk={counts['uk']}  pl={counts['pl']}  en={counts['en']}", end="  ")
    print("✓" if len(set(counts.values())) == 1 else "✗ РОЗБІЖНІСТЬ")

    texts = {}
    for lang in LANGS:
        ex = LangExtractor(lang)
        ex.feed(html)
        texts[lang] = ex.text()

    problems = []
    for line in texts["pl"].splitlines():
        if CYR.search(line):
            problems.append(("PL містить кирилицю", line))
    for line in texts["en"].splitlines():
        if CYR.search(line):
            problems.append(("EN містить кирилицю", line))
    for line in texts["uk"].splitlines():
        if POL.search(line) and "netlify" not in line.lower():
            problems.append(("UA містить польські літери", line))

    # однакові рядки в PL і UA — ознака невиконаного перекладу
    uk_lines = set(texts["uk"].splitlines())
    for line in texts["pl"].splitlines():
        if line in uk_lines and len(line) > 12 and not re.fullmatch(r'[\d\s\W]+', line):
            problems.append(("PL = UA (не перекладено)", line))

    if problems:
        print(f"\n❌ ЗНАЙДЕНО {len(problems)}:")
        for kind, line in problems:
            print(f"   [{kind}] {line[:100]}")
    else:
        print("\n✓ Кирилиці в PL/EN немає, неперекладених рядків немає")
    return texts

if __name__ == "__main__":
    all_texts = {}
    for path in sys.argv[1:]:
        all_texts[path] = check(path)
