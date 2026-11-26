import re

# Test the regex patterns with the actual content
hocr_content = """<span class='ocrx_word' id='word_1_1' title='bbox 0 2 100 20; x_wconf 85'>HELLO</span>"""

# Test the word extraction pattern
word_pattern = re.compile(r">(.*?)</span>", re.DOTALL | re.IGNORECASE)
span_content = "<span class='ocrx_word' id='word_1_1' title='bbox 0 2 100 20; x_wconf 85'>HELLO</span>"

match = word_pattern.search(span_content)
if match:
    print("Found match:", repr(match.group(1)))
else:
    print("No match found")

# Test with different patterns
print("Testing different patterns:")

# Pattern 1: capture everything between > and <
pattern1 = re.compile(r">(.*?)<", re.DOTALL)
result1 = pattern1.search(span_content)
print(f"Pattern 1: {result1}")

# Pattern 2: capture word text after the span
pattern2 = re.compile(r">(.*?)</span>", re.DOTALL | re.IGNORECASE)
result2 = pattern2.search(span_content)
print(f"Pattern 2: {result2}")

# Pattern 3: capture just the word text (no tags)
pattern3 = re.compile(r"[^>]+(?=<)", re.DOTALL)
result3 = pattern3.search(span_content)
print(f"Pattern 3: {result3}")

# Pattern 4: capture text between the span tags
pattern4 = re.compile(r">.*?>(.*?)<", re.DOTALL)
result4 = pattern4.search(span_content)
print(f"Pattern 4: {result4}")