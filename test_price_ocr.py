import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

import price_ocr

RED = (200, 40, 50)


def price_image(path, digits=3):
    # 赤い数字(幅20)×digits と「円」(幅30)の代わりに赤い四角を並べた画像
    image = Image.new('RGB', (400, 60), 'white')
    draw = ImageDraw.Draw(image)
    x = 20
    for _ in range(digits):
        draw.rectangle((x, 15, x + 19, 44), fill=RED)
        x += 30
    draw.rectangle((x, 15, x + 29, 44), fill=RED)
    image.save(path)


def tesseract_outputs(*texts):
    return [Mock(returncode=0, stdout=text + '\n', stderr='') for text in texts]


class ReadPriceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'price.png'
        price_image(self.path)
        patch('price_ocr.shutil.which', return_value='tesseract').start()
        self.run = patch('price_ocr.subprocess.run').start()

    def tearDown(self):
        patch.stopall()
        self.tmp.cleanup()

    def test_two_matching_reads_are_accepted_without_extra_passes(self):
        self.run.side_effect = tesseract_outputs('300', '300')
        result = price_ocr.read_price(self.path)
        self.assertTrue(result['ocr_agreement'])
        self.assertEqual(result['candidate_price_yen'], 300)
        self.assertEqual(result['digit_count'], 3)
        self.assertEqual(self.run.call_count, 2)

    def test_read_with_extra_digit_is_rejected_and_extra_passes_decide(self):
        # 実データ: 「３００円」を一方が 5300 と読んだ
        self.run.side_effect = tesseract_outputs('300', '5300', '300', '300')
        result = price_ocr.read_price(self.path)
        self.assertTrue(result['ocr_agreement'])
        self.assertEqual(result['candidate_price_yen'], 300)
        # 3回目で「桁数の合う300が2つ」になった時点で打ち切る
        self.assertEqual(self.run.call_count, 3)

    def test_agreeing_reads_with_wrong_digit_count_are_not_accepted(self):
        self.run.side_effect = tesseract_outputs('5300', '5300', '5300', '5300')
        result = price_ocr.read_price(self.path)
        self.assertFalse(result['ocr_agreement'])
        self.assertIsNone(result['candidate_price_yen'])

    def test_conflicting_reads_stay_unconfirmed(self):
        self.run.side_effect = tesseract_outputs('300', '800', '300', '800')
        result = price_ocr.read_price(self.path)
        self.assertFalse(result['ocr_agreement'])
        self.assertIsNone(result['candidate_price_yen'])

    def test_single_valid_read_is_not_enough(self):
        self.run.side_effect = tesseract_outputs('300', '5300', '', '5300')
        result = price_ocr.read_price(self.path)
        self.assertFalse(result['ocr_agreement'])


if __name__ == '__main__':
    unittest.main()
