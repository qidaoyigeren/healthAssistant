"""Synthetic printed discharge table: no real patient or treatment advice."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import pymupdf

out = Path('docs/product-upgrade/p5/samples')
out.mkdir(parents=True, exist_ok=True)
font = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 30)
large = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 42)
im = Image.new('RGB', (1600, 800), 'white')
d = ImageDraw.Draw(im)
d.text((65, 45), '合成示例：出院用药表（仅用于软件测试）', font=large, fill='black')
d.text((65, 120), '患者：local-demo    日期：2026-09-08', font=font, fill='black')
xs = [60, 420, 650, 850, 1250, 1540]
ys = [220, 310, 420, 530]
for x in xs:
    d.line((x, ys[0], x, ys[-1]), fill='#667085', width=2)
for y in ys:
    d.line((xs[0], y, xs[-1], y), fill='#667085', width=2)
rows = [['药名', '剂量', '单位', '频次', '途径'], ['氨氯地平', '5', 'mg', '每日一次', '口服'], ['阿司匹林', '100', 'mg', '每日一次', '口服']]
for ri, row in enumerate(rows):
    for ci, text in enumerate(row):
        d.text((xs[ci] + 20, ys[ri] + 22), text, font=font, fill='black')
d.text((65, 620), '此材料为合成测试数据，不构成用药建议。', font=font, fill='black')
im.save(out / 'clear-table.png')
im.rotate(3, expand=True, fillcolor='white').filter(ImageFilter.GaussianBlur(1.3)).save(out / 'tilted-blurred.png')
pdf = pymupdf.open()
page = pdf.new_page(width=800, height=400)
page.insert_image(page.rect, filename=str(out / 'clear-table.png'))
pdf.save(out / 'scanned-table.pdf')
second = pdf.new_page(width=800, height=400)
second.insert_image(second.rect, filename=str(out / 'clear-table.png'))
pdf.save(out / 'two-pages.pdf')
pdf.close()
print(out)
