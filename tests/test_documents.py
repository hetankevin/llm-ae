import importlib.util
import io
import unittest

from llm_ae.config import AEError
from llm_ae.documents import extract


@unittest.skipUnless(importlib.util.find_spec("pypdf"), "Install .[pdf] to test PDF extraction")
class PDFTest(unittest.TestCase):
    def pdf(self, text=True, encrypted=False):
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

        writer = PdfWriter()
        page = writer.add_blank_page(width=612, height=792)
        if text:
            font = DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                }
            )
            page[NameObject("/Resources")] = DictionaryObject(
                {
                    NameObject("/Font"): DictionaryObject(
                        {NameObject("/F1"): writer._add_object(font)}
                    )
                }
            )
            content = DecodedStreamObject()
            content.set_data(
                b"BT /F1 12 Tf 72 720 Td (We estimate the mean of independent observations under explicit finite variance assumptions. The sample mean is unbiased under these assumptions.) Tj ET"
            )
            page[NameObject("/Contents")] = writer._add_object(content)
        if encrypted:
            writer.encrypt("test-password")
        stream = io.BytesIO()
        writer.write(stream)
        return stream.getvalue()

    def test_extracts_pdf_text_with_page_anchors(self):
        text, warnings = extract(self.pdf(), "paper.pdf")
        self.assertIn("[Page 1]", text)
        self.assertIn("finite variance", text)
        self.assertTrue(any("equations" in w for w in warnings))

    def test_encrypted_pdf_requires_manual_action(self):
        with self.assertRaisesRegex(AEError, "Encrypted"):
            extract(self.pdf(encrypted=True), "paper.pdf")

    def test_image_only_pdf_does_not_get_a_scientific_assessment(self):
        with self.assertRaisesRegex(AEError, "Too little"):
            extract(self.pdf(text=False), "paper.pdf")

    def test_corrupt_pdf_fails_cleanly(self):
        with self.assertRaises(AEError):
            extract(b"This is not a PDF", "paper.pdf")


class DocumentTest(unittest.TestCase):
    def test_unsupported_documents_are_not_executed(self):
        with self.assertRaises(AEError):
            extract(b"print('no')" * 20, "paper.py")

    def test_invalid_utf8_is_not_silently_replaced(self):
        with self.assertRaises(AEError):
            extract(b"\xff" * 100, "paper.txt")
