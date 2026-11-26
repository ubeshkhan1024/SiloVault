import re

# Test parsing against the actual file
with open("test_debug.hocr", "r", encoding="utf-8") as f:
    content = f.read()

word_pattern = re.compile(
    r"<span\s+class=['\"]ocrx_word['\"]\s+[^>]*title=['\"]([^'\"]+)['\"]",
    re.IGNORECASE
)

matches = list(word_pattern.finditer(content))
print(f"Found {len(matches)} word matches")

for i, match in enumerate(matches):
    title = match.group(1)
    print(f"\nMatch {i}:")
    print(f"  Title: {title}")
    print(f"  Match group(0): {repr(match.group(0))}")
    print(f"  Match end: {match.end()}")
    
    after_span = content[match.end():]
    print(f"  After span (first 100 chars): {repr(after_span[:100])}")
    
    word_match = re.search(r">(.*?)</span>", after_span, re.DOTALL | re.IGNORECASE)
    print(f"  Word match: {word_match}")
    if word_match:
        print(f"  Word text: {repr(word_match.group(1))}")