"""Signal dependency integrity beyond the already-covered lifecycle cases."""
from copy import deepcopy
from datetime import date
from decimal import Decimal
import unittest

from backend.daily_inputs import freeze_inputs, input_matches, validate_inputs
from backend.learning_policy import build_frame
from tests import test_learning_integration as fixtures


class DailyInputsTests(unittest.TestCase):
    def setUp(self):
        self.data = fixtures.LearningIntegrationTests.completed_input()
        self.contract = freeze_inputs(self.data)

    def test_corrected_lookback_bar_blocks_even_when_all_features_are_equal(self):
        before = build_frame(self.data)
        symbol = self.data['rows'][0]['symbol']
        old_day = self.data['calendars']['KOSPI'][-61]
        self.data['histories'][symbol][old_day]['high'] += 1
        self.assertEqual(build_frame(self.data), before)
        self.assertFalse(validate_inputs(self.contract, self.data))

    def test_live_cache_decimal_prices_and_indices_use_the_same_numeric_hash(self):
        for history in self.data['histories'].values():
            for bar in history.values():
                for key, value in bar.items():
                    bar[key] = Decimal(str(value) + '.00')
        for history in self.data['benchmarks'].values():
            for day, value in history.items():
                history[day] = Decimal(str(value) + '.00')
        self.assertEqual(freeze_inputs(self.data), self.contract)
        self.assertTrue(validate_inputs(self.contract, self.data))

    def test_future_bars_are_not_part_of_signal_day_dependency_hash(self):
        future = date(2026, 10, 6)
        self.data['calendars']['KOSPI'].append(future)
        self.data['benchmarks']['KOSPI'][future] = 900
        for history in self.data['histories'].values():
            history[future] = {'open': 8000, 'high': 9000, 'low': 7000,
                               'close': 8000, 'volume': 2000, 'turnover': 16000000}
        self.assertTrue(validate_inputs(self.contract, self.data))

    def test_frozen_frame_and_contract_version_cannot_be_changed(self):
        for mutate in (lambda value: value['frame']['rows'][0].update(learning_eligible=False),
                       lambda value: value['universe'][0].update(board='KOSDAQ'),
                       lambda value: value['universe'][0].update(name='changed'),
                       lambda value: value.update(observed_at='2026-10-02T18:00:00+09:00'),
                       lambda value: value.update(version='unreviewed'),
                       lambda value: value.update(source_hash='changed')):
            with self.subTest(mutate=mutate):
                changed = deepcopy(self.contract)
                mutate(changed)
                self.assertFalse(validate_inputs(changed, self.data))

    def test_legacy_pending_still_rejects_eligibility_change(self):
        pending = {'market_input_hash': self.contract['hash']}
        self.data['rows'][0]['learning_eligible'] = False
        self.assertFalse(input_matches(pending, self.data))
        pending['signal_inputs'] = self.contract
        self.assertTrue(input_matches(pending, self.data))

    def test_dependency_price_loss_and_duplicate_rows_fail_closed(self):
        symbol = self.data['rows'][0]['symbol']
        old_day = self.data['calendars']['KOSPI'][-61]
        del self.data['histories'][symbol][old_day]
        self.assertFalse(validate_inputs(self.contract, self.data))
        self.data = fixtures.LearningIntegrationTests.completed_input()
        self.data['rows'].append(deepcopy(self.data['rows'][0]))
        self.assertFalse(validate_inputs(self.contract, self.data))

    def test_invalid_explicit_contract_is_not_treated_as_legacy(self):
        for value in (None, [], 'invalid'):
            self.assertFalse(validate_inputs(value, self.data))
            self.assertFalse(input_matches({'signal_inputs': value,
                'market_input_hash': self.contract['hash']}, self.data))
