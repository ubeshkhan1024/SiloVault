import re

# Simulate what the regex would see
hocr_content = """<span class='ocrx_word' id='word_1_1' title='bbox 0 2 100 20; x_wconf 85'>
          HELLO
        </span>"""

word_pattern = re.compile(
    r"<span\s+class=['\"]ocrx_word['\"]\s+[^>]*title=['\"]([^'\"]+)['\"]",
    re.IGNORECASE
)

matches = list(word_pattern.finditer(hocr_content))
print(f"Found {len(matches)} matches")

for i, match in enumerate(matches):
    print(f"\nMatch {i}:")
    print(f"  Full match: {repr(match.group(0))}")
    print(f"  Title: {repr(match.group(1))}")
    
    span_content = match.group(0)
    print(f"  span_content: {repr(span_content)}")
    
    word_match = re.search(r">(.*?)</span>", span_content, re.DOTALL | re.IGNORECASE)
    print(f"  word_match: {word_match}")
    if word_match:
        print(f"  word text: {repr(word_match.group(1))}")