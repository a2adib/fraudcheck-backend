"""FR-7 — courier risk scoring. Pure functions, no database, no clock."""

import pytest

from src.checks.enums import RiskBand
from src.checks.scoring import score_courier_history
from src.logistics.enums import ProviderEnum
from src.logistics.schemas import DeliveryStats


def stats(total: int, delivered: int, cancelled: int = 0) -> DeliveryStats:
    return DeliveryStats(
        total_orders=total,
        delivered=delivered,
        returned=max(total - delivered - cancelled, 0),
        cancelled=cancelled,
    )


def one(total: int, delivered: int, provider: ProviderEnum = ProviderEnum.PATHAO):
    return score_courier_history({provider: stats(total, delivered)})


class TestBands:
    def test_ac_7_1_a_spotless_history_scores_zero(self):
        """AC-7.1 — 20 delivered, 0 returned."""
        score = one(20, 20)

        assert score.score == 0
        assert score.band is RiskBand.LOW

    def test_ac_7_2_an_all_returns_history_scores_one_hundred(self):
        """AC-7.2 — 0 delivered, 20 returned."""
        score = one(20, 0)

        assert score.score == 100
        assert score.band is RiskBand.HIGH

    def test_ac_7_3_a_thin_file_is_pulled_to_neutral(self):
        """AC-7.3 — one delivered and one returned is a 50% rate on no evidence."""
        score = one(2, 1)

        assert score.score == 50
        assert score.confidence == 0.2
        assert score.band is RiskBand.MEDIUM

    def test_ac_7_4_no_history_at_all_is_neutral_and_flagged(self):
        """AC-7.4 — the honest answer is "we don't know", not "clean"."""
        score = score_courier_history({ProviderEnum.PATHAO: stats(0, 0)})

        assert score.score == 50
        assert score.insufficient_data is True
        assert score.band is RiskBand.MEDIUM

    def test_no_providers_answered_at_all_is_also_neutral(self):
        """AC-6.5 — every provider down still has to produce a score."""
        score = score_courier_history({})

        assert (score.score, score.insufficient_data) == (50, True)


class TestBreakdown:
    def test_ac_7_5_the_result_carries_its_own_arithmetic(self):
        """AC-7.5 — every input to the score is in the output."""
        score = one(10, 6)

        assert score.delivered == 6
        assert score.returned == 4
        assert score.total == 10
        assert score.return_ratio == 0.4
        assert score.confidence == 1.0
        assert score.contributing_providers == [ProviderEnum.PATHAO]

    def test_cancellations_count_as_returns(self):
        """A cancelled COD order is a failed delivery from the merchant's side."""
        score = score_courier_history({ProviderEnum.PATHAO: stats(10, 6, cancelled=2)})

        assert score.returned == 4

    def test_ac_7_7_a_provider_that_did_not_answer_is_absent(self):
        """AC-7.7 — the orchestrator passes only ``ok`` legs, so silence never scores as clean."""
        score = score_courier_history({ProviderEnum.PATHAO: stats(10, 5)})

        assert ProviderEnum.REDX not in score.contributing_providers
        assert [item.provider for item in score.breakdown] == [ProviderEnum.PATHAO]


class TestDisagreement:
    def test_ac_7_6_materially_different_provider_stories_are_flagged(self):
        """AC-7.6 — 90% returns at one courier and 10% at another is worth a warning."""
        score = score_courier_history(
            {
                ProviderEnum.PATHAO: stats(10, 1),
                ProviderEnum.REDX: stats(10, 9),
            }
        )

        assert score.uncertain is True

    def test_agreeing_providers_are_not_flagged(self):
        score = score_courier_history(
            {ProviderEnum.PATHAO: stats(10, 8), ProviderEnum.REDX: stats(10, 9)}
        )

        assert score.uncertain is False

    def test_a_provider_with_too_little_history_does_not_get_a_vote(self):
        """Two providers each holding one order will "disagree" by 100 points and mean nothing."""
        score = score_courier_history(
            {ProviderEnum.PATHAO: stats(10, 9), ProviderEnum.REDX: stats(1, 0)}
        )

        assert score.uncertain is False

    def test_a_single_provider_cannot_disagree_with_itself(self):
        assert one(10, 1).uncertain is False


class TestDeterminism:
    def test_ac_7_8_identical_inputs_score_identically_every_time(self):
        """AC-7.8 — byte-identical, a hundred times over."""
        results = {
            score_courier_history(
                {ProviderEnum.PATHAO: stats(7, 3), ProviderEnum.REDX: stats(11, 9)}
            ).model_dump_json()
            for _ in range(100)
        }

        assert len(results) == 1

    @pytest.mark.parametrize(
        ("total", "delivered", "band"),
        [
            (20, 20, RiskBand.LOW),
            (20, 15, RiskBand.LOW),  # 25% returns lands exactly on the low/medium edge
            (20, 10, RiskBand.MEDIUM),
            (20, 5, RiskBand.HIGH),
        ],
    )
    def test_band_boundaries(self, total: int, delivered: int, band: RiskBand):
        assert one(total, delivered).band is band
