import re

hocr_content = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Output</title></head>
<body>
<div class='ocr_page' id='page_1' title='image "input.png"; bbox 0 0 640 480; ppagenum 1'>
  <div class='ocr_carea' id='block_1_1' title='bbox 0 0 640 20'>
    <p class='ocr_par' id='par_1_1' lang='eng' title='basefont 1 12 11.00'>
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

# Find all ocrx_word spans
word_pattern = re.compile(
    r"<span\s+class=['\"]ocrx_word['\"]\s+[^>]*title=['\"]([^'\"]+)['\"]",
    re.IGNORECASE
)

for match in word_pattern.finditer(hocr_content):
    title = match.group(1)
    print(f"Title: {title}")
    
    # Get the position of this match in the full content
    start_pos = match.start()
    end_pos = match.end()
    print(f"Match span: {start_pos}-{end_pos}")
    print(f"Context: {repr(hocr_content[start_pos:end_pos+200])}")
    
    # Try to find word text after the span
    after_span = hocr_content[end_pos:]
    print(f"After span: {repr(after_span[:100])}")
    
    # Try different patterns
    word_match = re.search(r">(.*?)</span>", after_span, re.IGNORECASE)
    print(f"Word match: {word_match}")
    if word_match:
        print(f"Word text: {repr(word_match.group(1))}")
    print("---")