import base64
import asyncio
import unittest
from unittest.mock import patch

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

    def test_decodes_wrapped_data_url_and_repairs_padding(self):
        raw = b"\xff\xd8\xff" + b"jpeg-data"
        encoded = base64.b64encode(raw).decode("ascii").rstrip("=")
        wrapped = " data:image/jpeg;base64,\n  " + encoded[:7] + "\r\n\t" + encoded[7:] + " "

        decoded, filename = InvenTreeClient._decode_image(wrapped, "photo.jpg")

        self.assertEqual(decoded, raw)
        self.assertEqual(filename, "photo.jpg")

    def test_decodes_wrapped_plain_base64(self):
        raw = b"\x89PNG\r\n\x1a\n" + b"png-data"
        encoded = base64.b64encode(raw).decode("ascii")

        decoded, filename = InvenTreeClient._decode_image(
            encoded[:8] + "\n" + encoded[8:], "photo.png"
        )

        self.assertEqual(decoded, raw)
        self.assertEqual(filename, "photo.png")

    def test_rejects_non_image_base64(self):
        encoded = base64.b64encode(b"not an image").decode("ascii")

        with self.assertRaisesRegex(ValueError, "Unsupported image format"):
            InvenTreeClient._decode_image(encoded, "photo.png")

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

    def test_native_file_download_preserves_valid_original_bytes(self):
        raw = b"\x89PNG\r\n\x1a\n" + b"original-png-data"
        image_file = {
            "download_url": "https://files.example.test/photo",
            "file_id": "file_123",
            "mime_type": "image/png",
            "file_name": "board.jpg",
        }
        client = object.__new__(InvenTreeClient)

        with patch.object(InvenTreeClient, "_download_file_bytes", return_value=raw):
            decoded, filename = asyncio.run(
                client._download_image_file(image_file, "fallback.jpg")
            )

        self.assertEqual(decoded, raw)
        self.assertEqual(filename, "board.png")

    def test_native_file_rejects_non_image_mime_type(self):
        client = object.__new__(InvenTreeClient)
        image_file = {
            "download_url": "https://files.example.test/photo",
            "file_id": "file_123",
            "mime_type": "text/plain",
            "file_name": "photo.png",
        }

        with self.assertRaisesRegex(ValueError, "image MIME type"):
            asyncio.run(client._download_image_file(image_file, "fallback.jpg"))

    def test_native_file_rejects_non_image_bytes(self):
        client = object.__new__(InvenTreeClient)
        image_file = {
            "download_url": "https://files.example.test/photo",
            "file_id": "file_123",
            "mime_type": "image/png",
            "file_name": "photo.png",
        }

        with patch.object(
            InvenTreeClient, "_download_file_bytes", return_value=b"not an image"
        ), self.assertRaisesRegex(ValueError, "Unsupported image format"):
            asyncio.run(client._download_image_file(image_file, "fallback.jpg"))

    def test_native_file_rejects_private_or_non_https_url(self):
        for url in ("http://example.com/photo.png", "https://127.0.0.1/photo.png"):
            with self.subTest(url=url), self.assertRaisesRegex(
                ValueError, "public HTTPS|public addresses"
            ):
                InvenTreeClient._validate_public_https_url(url)


if __name__ == "__main__":
    unittest.main()
