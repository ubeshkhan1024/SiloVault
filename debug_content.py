import re

# Test the actual content extraction
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

# Test what the parsing function sees
word_pattern = re.compile(
    r"<span\s+class=['\"]ocrx_word['\"]\s+[^>]*title=['\"]([^'\"]+)['\"]",
    re.IGNORECASE
)

matches = list(word_pattern.finditer(hocr_content))
print(f"Found {len(matches)} word matches")

for i, match in enumerate(matches):
    title = match.group(1)
    print(f"Title: {title}")
    
    # Extract span content
    span_start = hocr_content.find("<span")
    span_end = match.end()
    span_content = hocr_content[span_start:match.end()]
    print(f"Span content: {repr(span_content)}")
    
    # Try to extract word text
    word_match = re.search(r">(.*?)</span>", span_content, re.DOTALL | re.IGNORECASE)
    if word_match:
        print(f"Word text: {repr(word_match.group(1))}")
    else:
        print("No word text found")