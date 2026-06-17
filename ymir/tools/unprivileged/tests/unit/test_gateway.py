"""Unit tests for unprivileged gateway."""

from flexmock import flexmock


class TestGatewaySharedOptions:
    def test_all_tools_share_options_dict(self):
        registered_tools = []

        mock_server = flexmock()
        mock_server.should_receive("register_many").once().replace_with(
            lambda tools: registered_tools.extend(tools)
        )
        mock_server.should_receive("serve").once()

        import ymir.tools.unprivileged.gateway as gateway_module

        flexmock(gateway_module, MCPServer=lambda config: mock_server)
        flexmock(gateway_module).should_receive("setup_logging").once()
        flexmock(gateway_module).should_receive("apply_zstream_override_from_env").once()

        gateway_module.main()

        assert registered_tools, "No tools were registered"
        first_options = registered_tools[0].options
        assert first_options is not None
        for tool in registered_tools[1:]:
            assert tool.options is first_options, (
                f"{type(tool).__name__} does not share the same options dict"
            )
