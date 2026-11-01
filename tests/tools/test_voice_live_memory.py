"""Voice creation carries the owning profile's sanitized, bounded memory snapshot."""
import asyncio
import json

from hermes_cli.web_routers.audio import _run_config_scoped
from hermes_cli import web_server_profiles
from tools.voice_live import build_session_config


def test_voice_creation_keeps_profile_context_isolated(tmp_path, monkeypatch):
    homes = {name: tmp_path / name for name in ("a", "b")}
    for name, home in homes.items():
        (home / "memories").mkdir(parents=True)
        (home / "memories" / "USER.md").write_text(f"User identity {name}")
        (home / "memories" / "MEMORY.md").write_text(f"Remembered project {name}")
        (home / "config.yaml").write_text(json.dumps({"voice": {"gpt_live": {
            "instructions": f"Profile name {name}; speak German."}}}))
    monkeypatch.setenv("HERMES_HOME", str(homes["a"]))
    monkeypatch.setattr(web_server_profiles, "_resolve_profile_dir", lambda name: homes[name])
    monkeypatch.setattr(web_server_profiles, "get_process_hermes_home", lambda: homes["a"])
    history = [{"type": "message", "role": "user", "content": []}]

    async def create():
        return [await _run_config_scoped(name, lambda: build_session_config(history))
                for name in ("a", "b", "a")]

    configs = asyncio.run(create())
    for name, config in zip(("a", "b", "a"), configs):
        other = "b" if name == "a" else "a"
        instructions = config["instructions"]
        assert f"User identity {name}" in instructions
        assert f"Remembered project {name}" in instructions
        assert f"User identity {other}" not in instructions
        assert f"Remembered project {other}" not in instructions
        assert instructions.endswith(f"Profile name {name}; speak German.")
        assert config["input"] == history
        assert config["delegation"] == {"type": "client"}
    assert configs[0] == configs[2]


def test_disabled_and_oversized_memory_do_not_leak_or_overfill_voice(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "memories").mkdir()
    (tmp_path / "memories" / "USER.md").write_text("Disabled private user fact")
    (tmp_path / "memories" / "MEMORY.md").write_text("Project fact " + "x" * 12000 + "END_OF_MEMORY")
    (tmp_path / "config.yaml").write_text(json.dumps({"memory": {"user_profile_enabled": False}}))
    config = build_session_config()
    instructions = config["instructions"]
    assert "Disabled private user fact" not in instructions
    assert "Project fact" in instructions
    assert "END_OF_MEMORY" not in instructions
    assert "Excerpt truncated" in instructions
    assert len(instructions) < 6500
