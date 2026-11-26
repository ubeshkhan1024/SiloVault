import re

# Simulate the actual _parse_hocr function
def _parse_hocr_debug(hocr_path):
    text_parts = []
    bounding_boxes = []
    
    with open(hocr_path, "r", encoding="utf-8") as f:
        content = f.read()

    word_pattern = re.compile(
        r"<span\s+class=['\"]ocrx_word['\"]\s+[^>]*title=['\"]([^'\"]+)['\"]",
        re.IGNORECASE
    )

    matches = list(word_pattern.finditer(content))
    print(f"Found {len(matches)} word matches")

    for match in matches:
        title = match.group(1)
        print(f"Title: {title}")
        
        bbox_match = re.search(r"bbox\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)", title, re.IGNORECASE)
        if not bbox_match:
            print(f"  No bbox found")
            continue

        x1, y1, x2, y2 = map(int, bbox_match.groups())
        if x2 <= x1 or y2 <= y1:
            print(f"  Invalid bbox: x2<=x1 or y2<=y1, skipping")
            continue
            
        w = x2 - x1
        h = y2 - y1

        # Extract word text from the span content
        span_content = match.group(0)
        word_match = re.search(r">(.*?)</span>", span_content, re.DOTALL | re.IGNORECASE)
        if not word_match:
            print(f"  No word text found in span")
            continue

        word_text = word_match.group(1).strip()
        print(f"  Word text: {repr(word_text)}")
        if word_text:
            text_parts.append(word_text)

        bounding_boxes.append({"x": x1, "y": y1, "w": w, "h": h})

    full_text = " ".join(text_parts)
    print(f"Full text: {repr(full_text)}")
    print(f"Bounding boxes: {bounding_boxes}")
    return full_text, bounding_boxes

# Create test HOCR content
hocr_content = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Output</title></head>
<body>
<div class='ocr_page' id='page_1' title='image "input.png"; bbox 0 0 640 480; ppagenum 1'>
  <div class='ocr_carea' id='block_1_1'>
    <p class='ocr_par' id='par_1_1' lang='eng'>
      <span class='ocr_line' id='line_1_1'>
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

hocr_path = "test_hocr.html"
with open(hocr_path, "w", encoding="utf-8") as f:
    f.write(hocr_content)

text, boxes = _parse_hocr_debug(hocr_path)
print(f"Result: text={repr(text)}, boxes={boxes}")