import os

import pytest
import requests
from flexmock import flexmock

import copr_tools
from copr_tools import build_package, _extract_package_name_regex


@pytest.mark.parametrize("srpm_path,expected_package", [
    ("/srpms/test-srpm-1.11.0-1.el9.src.rpm", "test-srpm"),
    ("/srpms/packit-1.11.0-1.fc43.src.rpm", "packit"),
])
def test_extract_package_name_regex(srpm_path, expected_package):
    assert _extract_package_name_regex(srpm_path) == expected_package
    
@pytest.mark.asyncio
async def test_build_package_kerberos_ticket_issue():
    flexmock(copr_tools).should_receive("init_kerberos_ticket").and_return(False).once()
    
    mock_ctx = flexmock()
    
    result = await build_package(
        project="test-project",
        chroots=["rhel-10.dev-x86_64"],
        srpm_path="/srpms/test-srpm-1.11.0-1.el9.src.rpm",
        ctx=mock_ctx,
    )
    
    assert result == ("failed", "Failed to initialize Kerberos ticket")

@pytest.mark.asyncio
async def test_build_package_project_already_exists_issue():
    flexmock(copr_tools).should_receive("init_kerberos_ticket").and_return(True).once()
    
    # Mock ProjectProxy class and its add method
    mock_project_proxy = flexmock()
    mock_project_proxy.should_receive("add").and_raise(Exception("Project already exists")).once()
    flexmock(copr_tools.ProjectProxy).new_instances(mock_project_proxy)
    
    mock_ctx = flexmock()
    mock_ctx.should_receive("info").replace_with(lambda x: None)
    
    result = await build_package(
        project="test-project",
        chroots=["rhel-10.dev-x86_64"],
        srpm_path="/srpms/test-srpm-1.11.0-1.el9.src.rpm",
        ctx=mock_ctx,
    )
    
    assert result == ("failed", "Copr project already exists")


@pytest.mark.asyncio
@pytest.mark.parametrize("build_state, urls", [
    ("succeeded", ["https://copr.devel.redhat.com/coprs/jotnar-bot/test-project/rhel-10.dev-x86_64/00123456-test-srpm"]),
    ("failed", ["https://copr.devel.redhat.com/coprs/jotnar-bot/test-project/rhel-10.dev-x86_64/00123456-test-srpm"]),
])
async def test_build_package_with_urls(build_state, urls):
    flexmock(copr_tools).should_receive("init_kerberos_ticket").and_return(True).once()
    
    # Mock ProjectProxy
    mock_project_proxy = flexmock()
    mock_project_proxy.should_receive("add").and_return(None).once()
    flexmock(copr_tools.ProjectProxy).new_instances(mock_project_proxy)
    
    # Mock BuildProxy
    mock_build = flexmock(id="123456")
    mock_build_proxy = flexmock()
    mock_build_proxy.should_receive("create_from_file").and_return(mock_build).once()
    
    # Mock successful build result
    mock_result = flexmock(state=build_state, repo_url="https://copr.devel.redhat.com/coprs/jotnar-bot/test-project")
    mock_build_proxy.should_receive("get").and_return(mock_result).once()
    flexmock(copr_tools.BuildProxy).new_instances(mock_build_proxy)
    
    async def mock_async_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)
    
    flexmock(copr_tools.asyncio).should_receive("to_thread").replace_with(mock_async_to_thread).times(3) # add, create_from_file, get
    
    async def mock_async_function(*args, **kwargs):
        return None

    mock_ctx = flexmock()
    mock_ctx.should_receive("info").replace_with(mock_async_function)
    mock_ctx.should_receive("report_progress").replace_with(mock_async_function)
    
    result = await build_package(
        project="test-project",
        chroots=["rhel-10.dev-x86_64"],
        srpm_path="/srpms/test-srpm-1.11.0-1.el9.src.rpm",
        ctx=mock_ctx,
    )
    
    assert result == (build_state, urls)


