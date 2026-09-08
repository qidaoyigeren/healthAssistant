# P5：真实本地 OCR

首版支持清晰打印版中文出院用药表，包含药名、剂量、单位、频次表头。PNG/JPEG/PDF 以内容校验格式，限制 6 MB、PDF 1–3 页、每页 1600 万像素，拒绝加密文档和多帧图片。原件按哈希存于当前患者的 SQLite 文档对象，不接受客户端文件路径，也不自动删除已引用原件。

使用 RapidOCR 3.9.2 + ONNX Runtime CPU，PyMuPDF 用于有界 PDF 渲染，Pillow 用于图片验证。安装：`uv pip install --python .venv/Scripts/python.exe -r requirements-product-ocr.txt`。这些依赖缺失时 CSV 仍可使用。

选型依据：[RapidOCR 安装](https://rapidai.github.io/RapidOCRDocs/main/en/install_usage/rapidocr/install/)、[官方 Python 使用说明](https://rapidai.github.io/RapidOCRDocs/main/install_usage/rapidocr/usage/)、[PyMuPDF OCR 说明](https://pymupdf.readthedocs.io/en/latest/recipes-ocr.html)。本机没有 Tesseract；实际扫描样本的原生 PDF 文本提取为 0 字符，RapidOCR 能识别目标字段，因此使用轻量 CPU OCR，未引入 Docling 全格式流水线。

解析在患者写锁外执行，独立持久化 job、尝试次数、租约、原始识别输出、模型指纹、解析器版本。失败可重试，进程中断后租约 180 秒到期可接管，最多 3 次。解析成功只创建 P2 候选；确认前必须对照原件核实 OCR 字段。

定位为真实 OCR polygon/bbox，坐标是页面渲染后左上角像素，PDF 2 倍缩放、旋转和图片 EXIF 处理写入元数据。日期/患者字段若共享一行，定位明确标为文本块粒度。前端并排展示原件与核对项，点击字段定位；原值、更正及报告确认分开保存。

`samples/` 的清晰图片、倾斜模糊图片、扫描 PDF、跨页 PDF 均为同一合成模板衍生的开发材料；`parser-comparison.json` 保存真实解析时间与字段统计。这不是独立盲测。错误主体、缺单位、不清小数、缺药名和解析失败另由反例测试覆盖。手写、药盒、复杂报告未支持，不外推准确率。
