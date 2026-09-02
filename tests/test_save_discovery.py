from planner.parser.saves import saves_root


def test_saves_root_honors_override(monkeypatch, tmp_path):
    target = tmp_path / "custom saves"
    monkeypatch.setenv("TR_SAVES_DIR", str(target))

    assert saves_root() == str(target)


def test_saves_root_infers_proton_prefix_from_game_dir(monkeypatch, tmp_path):
    library = tmp_path / "SteamLibrary"
    game_dir = library / "steamapps/common/Travellers Rest/Windows/TravellersRest_Data"
    saves = library / (
        "steamapps/compatdata/1139980/pfx/drive_c/users/steamuser/AppData/"
        "LocalLow/Louqou/TravellersRest/GameSaves"
    )
    saves.mkdir(parents=True)
    monkeypatch.delenv("TR_SAVES_DIR", raising=False)
    monkeypatch.setenv("TR_GAME_DIR", str(game_dir))

    assert saves_root() == str(saves)
