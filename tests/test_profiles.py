from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ai_investing.profiles import (
    default_profile_matrix_entries,
    load_env_file,
    strategy_parameters_for_risk_profile,
)


class ProfileTests(unittest.TestCase):
    def test_strategy_parameters_for_risk_profiles_are_distinct(self) -> None:
        conservative = strategy_parameters_for_risk_profile("conservative")
        balanced = strategy_parameters_for_risk_profile("balanced")
        aggressive = strategy_parameters_for_risk_profile("aggressive")

        self.assertGreater(conservative.cash_buffer, balanced.cash_buffer)
        self.assertGreater(balanced.cash_buffer, aggressive.cash_buffer)
        self.assertLess(conservative.max_position_weight, aggressive.max_position_weight)
        self.assertEqual(aggressive.trend_window, 150)

    def test_load_env_file_unquotes_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / ".env.paper"
            path.write_text(
                'AI_INVESTING_PROFILE_NAME="Aggressive Alpha"\n'
                "AI_INVESTING_RISK_PROFILE=aggressive\n"
                "OPENAI_API_KEY=\n"
            )

            values = load_env_file(path)

        self.assertEqual(values["AI_INVESTING_PROFILE_NAME"], "Aggressive Alpha")
        self.assertEqual(values["AI_INVESTING_RISK_PROFILE"], "aggressive")
        self.assertEqual(values["OPENAI_API_KEY"], "")

    def test_default_profile_matrix_entries_have_expected_names(self) -> None:
        entries = default_profile_matrix_entries(Path("profiles"))
        self.assertEqual([entry.name for entry in entries], ["conservative", "balanced", "aggressive"])


if __name__ == "__main__":
    unittest.main()
