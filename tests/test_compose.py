"""Bounded template checks, not a replacement for Docker Compose validation."""
import json
from pathlib import Path
import re
import unittest


class ComposeTemplateTests(unittest.TestCase):
    def test_tmpfs_is_one_quoted_mount_with_all_hardening_options(self):
        template = (Path(__file__).resolve().parents[1]
                    / "deployment" / "compose.yaml").read_text()
        # Require this template's single-line, JSON-compatible YAML flow list.
        # JSON parsing keeps commas inside the quoted scalar; the original
        # unquoted YAML split the mount into four separate list entries.
        # This deliberately does not parse or validate arbitrary Compose YAML.
        values = re.findall(r"^    tmpfs:\s*(\[[^\n]*\])[ \t]*$",
                            template, flags=re.MULTILINE)
        self.assertEqual(len(values), 1, "Expected one inline tmpfs list")
        self.assertEqual(json.loads(values[0]),
                         ["/tmp:rw,noexec,nosuid,size=16m"])


if __name__ == "__main__":
    unittest.main()
