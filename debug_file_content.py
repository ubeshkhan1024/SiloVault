# Create test HOCR file and examine its content
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

# Write to test file
with open("test_debug.hocr", "w", encoding="utf-8") as f:
    f.write(hocr_content)

# Read and examine
with open("test_debug.hocr", "r", encoding="utf-8") as f:
    content = f.read()
    print("=== FULL FILE CONTENT ===")
    print(content)
    print("=== END FILE ===")