import pytest

from meditation_server.domain.questionnaires import (
    validate_questionnaire,
    validate_response,
)


def numeric_rating_definition(minimum: int, maximum: int) -> dict:
    return {
        "title": "Scale questionnaire",
        "scales": {"rating_scale": {"min": minimum, "max": maximum}},
        "questions": [
            {
                "id": "rating",
                "type": "rating",
                "prompt": "Choose a value",
                "required": True,
                "scale_ref": "rating_scale",
            }
        ],
    }


@pytest.mark.parametrize(
    ("minimum", "maximum"),
    [(0, 3), (1, 4), (0, 4), (1, 5)],
)
def test_numeric_four_and_five_point_scales_are_valid(
    minimum: int,
    maximum: int,
) -> None:
    definition = numeric_rating_definition(minimum, maximum)

    validate_questionnaire(definition)
    validate_response(definition, {"rating": minimum})
    validate_response(definition, {"rating": maximum})


def test_server_does_not_restrict_numeric_scale_to_four_or_five_points() -> None:
    definition = numeric_rating_definition(1, 6)

    validate_questionnaire(definition)
    validate_response(definition, {"rating": 6})


@pytest.mark.parametrize("answer", [-1, 4])
def test_numeric_scale_rejects_values_outside_its_range(answer: int) -> None:
    definition = numeric_rating_definition(0, 3)

    with pytest.raises(ValueError, match="not one of the scale's valid values"):
        validate_response(definition, {"rating": answer})


def test_labelled_options_scale_remains_supported() -> None:
    definition = {
        "title": "Labelled scale questionnaire",
        "scales": {
            "frequency": {
                "options": [
                    {"value": 0, "label": "Never"},
                    {"value": 2, "label": "Sometimes"},
                    {"value": 5, "label": "Always"},
                ]
            }
        },
        "questions": [
            {
                "id": "frequency",
                "type": "rating",
                "prompt": "How often?",
                "scale_ref": "frequency",
            }
        ],
    }

    validate_questionnaire(definition)
    validate_response(definition, {"frequency": 2})
    with pytest.raises(ValueError, match="not one of the scale's valid values"):
        validate_response(definition, {"frequency": 1})
