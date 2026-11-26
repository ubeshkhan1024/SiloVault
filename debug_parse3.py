import re

# Simulate what the _parse_hocr function does
hocr_content = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Output</title></head>
<body>
<div class='ocr_page' id='page_1' title='image "input.png"; bbox 0 0 640 480; ppagenum 1'>
  <div class='ocr_carea' id='block_1_1'>
    <p class='ocr_par' id='par_1_1' lang='eng'>
      <span class='ocr_line' id='line_1_1' title='bbox 0 0 640 20'>
        <span class='ocrx_word' id='word_1_1' title='bbox 0 2 100 20; x_wconf 85'>
          HELLO
        </span>
      </span>
    </p>
  </div>
</div>
</body>
</html>
"""

word_pattern = re.compile(
    r"<span\s+class=['\"]ocrx_word['\"]\s+[^>]*title=['\"]([^'\"]+)['\"]",
    re.IGNORECASE
)

matches = list(word_pattern.finditer(hocr_content))
print(f"Found {len(matches)} word matches")

for i, match in enumerate(matches):
    title = match.group(1)
    print(f"\nMatch {i}:")
    print(f"  Title: {title}")
    
    span_end = match.end()
    print(f"  span_end position: {span_end}")
    print(f"  span_end char: {repr(hocr_content[span_end:span_end+50])}")
    
    after_span = hocr_content[span_end:]
    print(f"  after_span (first 100 chars): {repr(after_span[:100])}")
    
    # Try to find the closing </span> and text before it
    close_span_match = re.search(r">(.*?)</span>", after_span, re.DOTALL | re.IGNORECASE)
    print(f"  close_span_match: {close_span_match}")
    if close_span_match:
        print(f"    Word text: {repr(close_span_match.group(1))}")