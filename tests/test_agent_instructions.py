from app.agent_instructions import agent_developer_instructions


def test_agent_instructions_describe_reviewed_pi_capabilities(monkeypatch):
    monkeypatch.setenv(
        "CEO_PROMPT_VAR_RESPONSIBILITY_SUMMARY",
        "星尘数据的CEO，负责算法部、售前部、市场部、HR部的工作。",
    )

    instructions = agent_developer_instructions()

    assert instructions.startswith(
        "You are the Pi-powered local CEO DingTalk reply worker."
    )
    assert "execute_reviewed_read" in instructions
    assert "--timeout 900" in instructions
    assert "Never run `dws auth login`" in instructions
    assert "DWS login/tool issue" in instructions
    assert "DingTalk mail handling" in instructions
    assert "dws_mail_reply" in instructions
    assert "DingTalk Todo intent routing" in instructions
    assert "dws todo task create" in instructions
    assert "Never call memory_write or document_upload for a DingTalk Todo" in instructions
    assert "星尘数据的CEO，负责算法部、售前部、市场部、HR部的工作。" in instructions
    assert "当前待处理消息" not in instructions


def test_agent_instructions_expose_xiaoqing_reads_but_not_decision_writes():
    instructions = agent_developer_instructions()

    assert "Xiaoqing interview material reading" in instructions
    assert "https://interview.hr.startask.net/candidates/" in instructions
    for tool in (
        "search_candidates",
        "get_dashboard_stats",
        "get_interview_context",
        "download_attachment",
        "list_candidate_interviews",
    ):
        assert tool in instructions
    assert "Do not call upload_interview_result" in instructions
    assert "This Pi runtime has no reviewed" not in instructions
    assert "critical_info_unavailable:xiaoqing_interview" in instructions


def test_agent_instructions_keep_authenticated_memory_acl_boundary():
    instructions = agent_developer_instructions()

    assert "user_get" in instructions
    assert "memory_recall" in instructions
    assert "memory_write" in instructions
    assert "document_upload" in instructions
    assert "Never pass user_id, graph_id, or graph_ids" in instructions
    assert "Authenticated ACL resolves scope" in instructions
    assert "critical_info_unavailable:memory_connector" in instructions


def test_agent_instructions_inject_work_profile_without_exposing_source_path(
    monkeypatch,
    tmp_path,
):
    profile = tmp_path / "work_profile.md"
    profile.write_text(
        "# Work Profile\n\n- Keep the loop tight.\n\n心智模型、决策启发式、表达DNA\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CEO_WORK_PROFILE_PATH", str(profile))

    instructions = agent_developer_instructions()

    assert "明哥 工作人格 Profile" in instructions
    assert "# Work Profile" in instructions
    assert "心智模型、决策启发式、表达DNA" in instructions
    assert "不要再尝试读取 profile 文件路径" in instructions
    assert str(profile) not in instructions
