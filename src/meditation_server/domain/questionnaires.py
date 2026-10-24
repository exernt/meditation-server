from __future__ import annotations

from typing import Any


QUESTION_TYPES = {"text", "single_choice", "multiple_choice", "rating"}


def validate_questionnaire(definition: dict[str, Any]) -> None:
    if not isinstance(definition, dict):
        raise ValueError("Questionnaire definition must be a JSON object")
    title = definition.get("title")
    questions = definition.get("questions")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("Questionnaire 'title' must be a non-empty string")
    if not isinstance(questions, list) or not questions:
        raise ValueError("Questionnaire 'questions' must be a non-empty list")
    scales = definition.get("scales", {})
    if not isinstance(scales, dict):
        raise ValueError("Questionnaire 'scales' must be an object")
    seen: set[str] = set()
    for question in questions:
        if not isinstance(question, dict):
            raise ValueError("Each question must be a JSON object")
        question_id = question.get("id")
        if not isinstance(question_id, str) or not question_id.strip():
            raise ValueError("Question 'id' must be a non-empty string")
        if question_id in seen:
            raise ValueError(f"Duplicate question id: {question_id!r}")
        seen.add(question_id)
        if not isinstance(question.get("prompt"), str) or not question["prompt"].strip():
            raise ValueError(f"{question_id}: 'prompt' must be a non-empty string")
        if "required" in question and not isinstance(question["required"], bool):
            raise ValueError(f"{question_id}: 'required' must be a boolean")
        kind = question.get("type")
        if kind not in QUESTION_TYPES:
            raise ValueError(f"Invalid question type: {kind!r}")
        if kind in {"single_choice", "multiple_choice"}:
            _validate_options(question)
        if kind == "rating":
            _validate_rating(question, scales)


def validate_response(definition: dict[str, Any], answers: dict[str, Any]) -> None:
    if not isinstance(answers, dict):
        raise ValueError(f"Response must be a dict of question_id -> answer, got {type(answers).__name__}")
    questions = {question["id"]: question for question in definition["questions"]}
    unknown = set(answers) - set(questions)
    if unknown:
        raise ValueError(f"Response references unknown question id(s): {sorted(unknown)}")
    scales = definition.get("scales", {})
    for question_id, question in questions.items():
        if question_id not in answers:
            if question.get("required", False):
                raise ValueError(f"Missing required answer for question '{question_id}'")
            continue
        value = answers[question_id]
        kind = question["type"]
        if kind == "text" and not isinstance(value, str):
            raise ValueError(f"{question_id}: expected a string, got {type(value).__name__}")
        if kind == "single_choice":
            _validate_choice(question, value)
        if kind == "multiple_choice":
            if not isinstance(value, list):
                raise ValueError(f"{question_id}: expected a list of selections, got {type(value).__name__}")
            for item in value:
                _validate_choice(question, item)
        if kind == "rating":
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"{question_id}: expected an integer, got {type(value).__name__}")
            scale = scales[question["scale_ref"]]
            valid = {item["value"] for item in scale["options"]} if "options" in scale else set(range(scale["min"], scale["max"] + 1))
            if value not in valid:
                raise ValueError(f"{question_id}: {value} is not one of the scale's valid values {sorted(valid)}")


def _validate_options(question: dict[str, Any]) -> None:
    options = question.get("options")
    if not isinstance(options, list) or not options:
        raise ValueError(f"{question['id']}: choice question requires a non-empty 'options' list")
    values: set[Any] = set()
    for option in options:
        if isinstance(option, str):
            if not option.strip():
                raise ValueError(f"{question['id']}: option strings must not be empty")
            value = option
        elif isinstance(option, dict):
            label = option.get("label")
            if not isinstance(label, str) or not label.strip():
                raise ValueError(f"{question['id']}: structured option requires a non-empty 'label'")
            if "allow_free_text" in option and not isinstance(option["allow_free_text"], bool):
                raise ValueError(f"{question['id']}: 'allow_free_text' must be a boolean")
            value = option.get("value", label)
        else:
            raise ValueError(f"{question['id']}: options must be strings or objects")
        if value in values:
            raise ValueError(f"{question['id']}: duplicate option value {value!r}")
        values.add(value)


def _validate_rating(question: dict[str, Any], scales: dict[str, Any]) -> None:
    reference = question.get("scale_ref")
    if not reference or reference not in scales:
        raise ValueError(f"{question['id']}: scale_ref '{reference}' not found in 'scales'")
    scale = scales[reference]
    if "options" in scale:
        options = scale["options"]
        if not isinstance(options, list) or not options:
            raise ValueError(f"{question['id']}: rating scale options must be a non-empty list")
        if any(not isinstance(item, dict) or type(item.get("value")) is not int or not isinstance(item.get("label"), str) for item in options):
            raise ValueError(f"{question['id']}: each rating option requires integer 'value' and 'label'")
    elif type(scale.get("min")) is not int or type(scale.get("max")) is not int or scale["min"] > scale["max"]:
        raise ValueError(f"{question['id']}: rating scale requires integer 'min' and 'max' with min <= max")


def _validate_choice(question: dict[str, Any], value: Any) -> None:
    if not isinstance(value, dict) or "value" not in value:
        raise ValueError(f"{question['id']}: expected {{'value': ..., 'detail': ...}}, got {value!r}")
    options = question["options"]
    valid = {(item.get("value", item.get("label")) if isinstance(item, dict) else item) for item in options}
    if value["value"] not in valid:
        raise ValueError(f"{question['id']}: '{value['value']}' is not one of the valid options")
    option = next((item for item in options if isinstance(item, dict) and item.get("value", item.get("label")) == value["value"]), None)
    if option and option.get("allow_free_text") and (not isinstance(value.get("detail"), str) or not value["detail"].strip()):
        raise ValueError(f"{question['id']}: option '{value['value']}' requires non-empty 'detail' text")
