from ymir.agents.backport_agent import _move_build_logs, _update_fix_attempts_log


class TestMoveBuildLogs:
    def test_moves_log_files(self, tmp_path):
        source = tmp_path / "source"
        source.mkdir()
        (source / "build.log").write_text("log content")
        (source / "root.log").write_text("root content")

        target = tmp_path / "target"
        _move_build_logs(source, target)

        assert (target / "build.log").exists()
        assert (target / "root.log").exists()
        assert not (source / "build.log").exists()
        assert not (source / "root.log").exists()

    def test_moves_gz_files(self, tmp_path):
        source = tmp_path / "source"
        source.mkdir()
        (source / "build.log.gz").write_bytes(b"\x1f\x8b fake gz")

        target = tmp_path / "target"
        _move_build_logs(source, target)

        assert (target / "build.log.gz").exists()
        assert not (source / "build.log.gz").exists()

    def test_ignores_non_log_files(self, tmp_path):
        source = tmp_path / "source"
        source.mkdir()
        (source / "package.spec").write_text("spec content")
        (source / "README.md").write_text("readme")
        (source / "build.log").write_text("log")

        target = tmp_path / "target"
        _move_build_logs(source, target)

        assert (target / "build.log").exists()
        assert not (target / "package.spec").exists()
        assert not (target / "README.md").exists()
        assert (source / "package.spec").exists()
        assert (source / "README.md").exists()

    def test_creates_target_directory(self, tmp_path):
        source = tmp_path / "source"
        source.mkdir()
        (source / "build.log").write_text("log")

        target = tmp_path / "nested" / "deep" / "target"
        _move_build_logs(source, target)

        assert target.exists()
        assert (target / "build.log").exists()

    def test_noop_when_no_logs(self, tmp_path):
        source = tmp_path / "source"
        source.mkdir()
        (source / "README.md").write_text("no logs here")

        target = tmp_path / "target"
        _move_build_logs(source, target)

        assert target.exists()
        assert list(target.iterdir()) == []


class TestUpdateFixAttemptsLog:
    def test_creates_log_on_first_attempt(self, tmp_path):
        _update_fix_attempts_log(tmp_path, 1, "undefined reference to 'foo'")

        log = tmp_path / "fix-attempts.md"
        assert log.exists()
        content = log.read_text()
        assert "# Fix Attempts Log" in content
        assert "## Initial build failure" in content
        assert "## Attempt 1" in content
        assert "undefined reference to 'foo'" in content

    def test_appends_on_subsequent_attempt(self, tmp_path):
        _update_fix_attempts_log(tmp_path, 1, "first error")
        _update_fix_attempts_log(tmp_path, 2, "second error")

        content = (tmp_path / "fix-attempts.md").read_text()
        assert "## Attempt 1" in content
        assert "## Attempt 2" in content
        assert "first error" in content
        assert "second error" in content

    def test_preserves_existing_content_on_append(self, tmp_path):
        _update_fix_attempts_log(tmp_path, 1, "original error")
        original_content = (tmp_path / "fix-attempts.md").read_text()

        _update_fix_attempts_log(tmp_path, 2, "new error")
        new_content = (tmp_path / "fix-attempts.md").read_text()

        assert new_content.startswith(original_content.rstrip())

    def test_error_wrapped_in_code_block(self, tmp_path):
        _update_fix_attempts_log(tmp_path, 1, "make[2]: *** [Makefile:42] Error 1")

        content = (tmp_path / "fix-attempts.md").read_text()
        assert "```\nmake[2]: *** [Makefile:42] Error 1\n```" in content
