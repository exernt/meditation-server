from __future__ import annotations

import unittest

from meditation_server.repositories.asyncpg import _compile_named_parameters


class NamedParameterCompilationTests(unittest.TestCase):
    def test_reuses_position_for_repeated_parameter_and_preserves_casts(self) -> None:
        sql, arguments = _compile_named_parameters(
            "SELECT :user_id::uuid, :user_id::uuid, :status",
            {"user_id": "abc", "status": "active"},
        )
        self.assertEqual(sql, "SELECT $1::uuid, $1::uuid, $2")
        self.assertEqual(arguments, ("abc", "active"))

    def test_parameter_mismatch_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing: name; unused: other"):
            _compile_named_parameters("SELECT :name", {"other": "value"})
