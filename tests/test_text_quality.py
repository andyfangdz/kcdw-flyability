import copy
import json
import unittest
from pathlib import Path
from kcdw.validation import validate_analysis, ValidationError

class TextQualityTests(unittest.TestCase):
    def setUp(self):
        self.analysis = json.loads(Path('tests/fixtures/sample_analysis.json').read_text())
        self.snapshot = json.loads(Path('tests/fixtures/sample_snapshot.json').read_text())

    def test_corrupt_prose_is_rejected(self):
        for text in ('TAFs support V干.', 'The front arrives 늦.', 'VFR\ufffd.', 'VFR\u202e.'):
            with self.subTest(text=text):
                data=copy.deepcopy(self.analysis)
                data['days'][0]['narrative']=text
                with self.assertRaises(ValidationError): validate_analysis(data,self.snapshot)

    def test_incomplete_narrative_is_rejected(self):
        self.analysis['days'][0]['narrative']='The proxy TAF supports V'
        with self.assertRaises(ValidationError): validate_analysis(self.analysis,self.snapshot)

    def test_normal_weather_punctuation_is_preserved(self):
        self.analysis['days'][0]['narrative']='VFR is likely with 5–10 kt winds, 20°C temperatures, and good visibility—check the pilot’s minimums.'
        validate_analysis(self.analysis,self.snapshot)
