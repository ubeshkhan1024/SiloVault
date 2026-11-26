import re

# Test with the actual HOCR content
hocr_content = """<span class='ocrx_word' id='word_1_1' title='bbox 0 2 100 20; x_wconf 85'>
  HELLO
</span>"""

word_pattern = re.compile(
    r"<span\s+class=['\"]ocrx_word['\"]\s+[^>]*title=['\"]([^'\"]+)['\"]",
    re.IGNORECASE
)

matches = list(word_pattern.finditer(hocr_content))
print(f"Found {len(matches)} word matches")

for match in matches:
    title = match.group(1)
    print(f"Title: {title}")
    
    span_content = match.group(0)
    print(f"Span content: {repr(span_content)}")
    
    # Try different patterns
    word_match1 = re.search(r">(.*?)</span>", span_content, re.DOTALL | re.IGNORECASE)
    print(f"Pattern 1 (DOTALL): {word_match1}")
    
    word_match2 = re.search(r">(.*?)<", span_content, re.DOTALL)
    print(f"Pattern 2: {word_match2}")
    
    # Try without DOTALL
    word_match3 = re.search(r">(.*?)</span>", span_content, re.IGNORECASE)
    print(f"Pattern 3 (no DOTALL): {word_match3}")