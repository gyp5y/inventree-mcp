import base64
import unittest

from client import InvenTreeClient


class DecodeImageTests(unittest.TestCase):
    def test_decodes_raw_png_base64_and_corrects_extension(self):
        raw = b"\x89PNG\r\n\x1a\n" + b"png-data"

        decoded, filename = InvenTreeClient._decode_image(
            base64.b64encode(raw).decode("ascii"), "chat-photo.jpg"
        )

        self.assertEqual(decoded, raw)
        self.assertEqual(filename, "chat-photo.png")

    def test_decodes_raw_jpeg_base64(self):
        raw = b"\xff\xd8\xff" + b"jpeg-data"

        decoded, filename = InvenTreeClient._decode_image(
            base64.b64encode(raw).decode("ascii"), "photo.jpeg"
        )

        self.assertEqual(decoded, raw)
        self.assertEqual(filename, "photo.jpeg")

    def test_decodes_png_data_url(self):
        raw = b"\x89PNG\r\n\x1a\n" + b"png-data"
        data_url = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")

        decoded, filename = InvenTreeClient._decode_image(data_url, "photo.png")

        self.assertEqual(decoded, raw)
        self.assertEqual(filename, "photo.png")

    def test_recognizes_gif_and_webp(self):
        gif = b"GIF89a" + b"gif-data"
        webp = b"RIFF" + (4).to_bytes(4, "little") + b"WEBPdata"

        self.assertEqual(
            InvenTreeClient._decode_image(base64.b64encode(gif).decode("ascii"), "photo.gif"),
            (gif, "photo.gif"),
        )
        self.assertEqual(
            InvenTreeClient._decode_image(base64.b64encode(webp).decode("ascii"), "photo.webp"),
            (webp, "photo.webp"),
        )


if __name__ == "__main__":
    unittest.main()
