"""pdms aws logs: CloudWatch lines of the dev Lambdas read with the AWS CLI, the events found in them (botocore's
SendMessage / Publish calls at DEBUG and what the broker receives), the filter AWS applies and following.

The lines have the shape of real ones from dev (made-up ids and data)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pdms_cli import actions, awsenv, awslogs, cli, events
from pdms_cli.config import Config, Defaults, Repo
from pdms_cli.ui import awslogs as ui_awslogs
from pdms_cli.ui.api import Api

HEAD = "broker_sqs_event 2026-10-19 15:53:36,251 9 MainThread"
CONTEXT = ("'url': 'https://sqs.us-east-1.amazonaws.com/', 'context': {'client_region': 'us-east-1', 'client_config': "
           "<botocore.config.Config object at 0x7fe4d44c7ac0>, 'has_streaming_input': False}}")
EVENT = {"entity_id": None, "event_id": "11111111-2222-3333-4444-555555555555", "type": "lead-tp-export",
         "app_context": None, "file_name": "tp.csv", "filter_schema": {"status": ["PROSPECTING"]}}


def botocore(operation: str, body: str, head: str = HEAD) -> str:
    return (f"{head} DEBUG botocore.endpoint:114 Making request for OperationModel(name={operation}) with params: "
            f"{{'url_path': '/', 'query_string': '', 'method': 'POST', 'headers': {{'X-Amz-Target': "
            f"'AmazonSQS.{operation}'}}, 'body': {body}, {CONTEXT}")


def sqs_send(event: dict, queue: str = "lead_tp_export-sqs-queue.fifo") -> str:
    payload = {"QueueUrl": f"https://sqs.us-east-1.amazonaws.com/111122223333/{queue}",
               "MessageBody": json.dumps(event), "MessageGroupId": "g-1"}
    return botocore("SendMessage", repr(json.dumps(payload).encode()))


def sns_publish(event: str, data: dict, topic: str = "sns-account-publish.fifo") -> str:
    form = {"Action": "Publish", "Version": "2010-03-31", "TopicArn": f"arn:aws:sns:us-east-1:111122223333:{topic}",
            "Message": json.dumps({"event": event, "data": json.dumps(data)}), "MessageGroupId": "g-2"}
    return botocore("Publish", repr(form), head="account_publish_ev 2026-10-15 20:04:01,544 16 MainThread")


def broker_received(*bodies: dict) -> str:
    items = ", ".join(f"MessageSQS(entity_id='m-{i}', created_at=datetime.datetime(2026, 10, 5, 14, 40, 35, 625095), "
                      f"body={body!r})" for i, body in enumerate(bodies))
    return (f"{HEAD} INFO broker_sqs_event.presentation.entry:72 Process events: entity_id=None "
            f"created_at=datetime.datetime(2026, 10, 5, 14, 40, 35, 625110) items=[{items}]")


def cloudwatch(message: str, at: int = 1_000_000, stream: str = "s1", ident: str = "") -> dict:
    return {"timestamp": at, "message": message + "\n", "logStreamName": stream, "eventId": ident or f"e{at}{stream}"}


def parsed(message: str) -> awslogs.Line:
    return awslogs.parse(cloudwatch(message), "broker-sqs-event")


# --------------------------------------------------------------------------- reading a line


def test_an_sqs_send_gives_its_queue_and_its_event() -> None:
    line = parsed(sqs_send(EVENT))
    assert line.level == "DEBUG" and line.logger == "broker_sqs_event" and line.where == "botocore.endpoint:114"
    [message] = line.messages
    assert (message.direction, message.operation, message.kind) == ("sent", "SendMessage", "sqs")
    assert message.destination == "lead_tp_export-sqs-queue.fifo"
    assert message.type == "lead-tp-export" and message.body == EVENT


def test_an_sns_publish_gives_its_topic_and_its_data_decoded() -> None:
    line = awslogs.parse(cloudwatch(sns_publish("TP_ACCOUNT_UPDATED", {"entity_id": "a-1", "account": {"npi": "1"}})),
                         "account-publish-ev")
    [message] = line.messages
    assert (message.kind, message.destination, message.type) == ("sns", "sns-account-publish.fifo", "TP_ACCOUNT_UPDATED")
    assert message.body == {"event": "TP_ACCOUNT_UPDATED", "data": {"entity_id": "a-1", "account": {"npi": "1"}}}


def test_batches_give_one_message_per_entry() -> None:
    sqs = {"QueueUrl": "https://sqs.us-east-1.amazonaws.com/1/q.fifo",
           "Entries": [{"Id": "1", "MessageBody": json.dumps({"type": "a"})},
                       {"Id": "2", "MessageBody": json.dumps({"type": "b"})}]}
    assert [m.type for m in parsed(botocore("SendMessageBatch", repr(json.dumps(sqs).encode()))).messages] == ["a", "b"]
    sns = {"Action": "PublishBatch", "TopicArn": "arn:aws:sns:us-east-1:1:t.fifo",
           "PublishBatchRequestEntries.member.1.Id": "1",
           "PublishBatchRequestEntries.member.1.Message": json.dumps({"event": "X"}),
           "PublishBatchRequestEntries.member.2.Message": json.dumps({"event": "Y"})}
    found = parsed(botocore("PublishBatch", repr(sns))).messages
    assert [(m.destination, m.type) for m in found] == [("t.fifo", "X"), ("t.fifo", "Y")]


def test_what_the_broker_receives_is_each_body_of_process_events() -> None:
    nested = {"type": "websocket-notify", "action_type": "IMPORT_FLOW_TASK_RUN", "body": {"status": "LOADING"}}
    found = parsed(broker_received(EVENT, nested)).messages
    assert [(m.direction, m.type) for m in found] == [("received", "lead-tp-export"), ("received", "websocket-notify")]
    assert found[1].body == nested  # its own "body" key is not one more message


def test_other_botocore_calls_cut_lines_and_plain_text_give_no_message() -> None:
    assert parsed(botocore("GetQueueUrl", "b'{}'")).messages == []
    cut = sqs_send(EVENT)[:400]  # CloudWatch keeps at most 256 KB of a line
    assert parsed(cut).messages == []
    assert parsed(f"{HEAD} INFO x:1 Process events: items=[]").messages == []
    assert parsed("body={'not': 'a log line'}").messages == []


def test_a_body_that_is_not_json_is_kept_as_text() -> None:
    message = awslogs.sent("SendMessage", {"QueueUrl": "https://x/q", "MessageBody": "plain text"})[0]
    assert (message.destination, message.body, message.type) == ("q", "plain text", "")


def test_runtime_lines_give_the_invocation_and_tracebacks_stay_with_their_line() -> None:
    request = "9eaf5169-5535-5a9e-a9ea-32302a2035e7"
    raw = [
        cloudwatch(f"START RequestId: {request} Version: $LATEST", 1000),
        cloudwatch(f"{HEAD} ERROR broker_sqs_event.presentation.entry:90 Failed event broker: boom", 1100),
        cloudwatch("Traceback (most recent call last):", 1101),
        cloudwatch('  File "/var/task/entry.py", line 86, in route_events', 1102),
        cloudwatch("[ERROR]\t2026-10-19T15:53:37.368Z\t" + request + "\tKeyError: 'type'", 1200),
        cloudwatch("an orphan line much later", 9000),
        cloudwatch(f"END RequestId: {request}", 9100),
        cloudwatch(f"REPORT RequestId: {request}\tDuration: 213.53 ms", 9101),
        cloudwatch("INIT_START Runtime Version: python:3.12", 50, stream="s2"),
    ]
    lines = awslogs.assemble([awslogs.parse(event, "broker-sqs-event") for event in reversed(raw)])
    assert [line.text.split(" ")[0] for line in lines] == ["INIT_START", "START", "broker_sqs_event",
                                                          "[ERROR]\t2026-10-19T15:53:37.368Z\t" + request + "\tKeyError:",
                                                          "an", "END", "REPORT"]
    failed = lines[2]
    assert failed.level == "ERROR" and failed.request == request
    assert failed.more == ["Traceback (most recent call last):", '  File "/var/task/entry.py", line 86, in route_events']
    assert "\nTraceback" in failed.raw
    assert lines[3].level == "ERROR" and lines[3].request == request
    assert lines[4].level == "" and lines[4].more == []  # too long after: its own line
    assert lines[0].platform and lines[0].request == ""  # another stream, no START read
    assert lines[6].request == request


# --------------------------------------------------------------------------- what to read


def test_the_filter_pattern_keeps_event_lines_and_errors_unless_a_text_or_type_is_asked() -> None:
    assert awslogs.pattern() == ('?"OperationModel(name=SendMessage" ?"OperationModel(name=Publish" '
                                 '?"Process events" ?"ERROR"')
    assert awslogs.pattern(events_only=False) == ""
    assert awslogs.pattern('flow "b786"', "lead-tp-export") == '"flow b786" "lead-tp-export"'
    assert awslogs.pattern(" ", "") == awslogs.pattern()


def test_keep_says_what_the_filter_of_aws_cannot() -> None:
    event, plain = parsed(sqs_send(EVENT)), parsed(f"{HEAD} INFO inject:270 Created a binding")
    error = parsed(f"{HEAD} ERROR entry:90 Failed event broker: boom")
    assert awslogs.keep(event) and awslogs.keep(error) and not awslogs.keep(plain)
    assert awslogs.keep(plain, events_only=False)
    assert awslogs.keep(event, event_type="lead-tp-export") and not awslogs.keep(event, event_type="lead-tp")
    assert awslogs.keep(error, level="error") and not awslogs.keep(event, level="error")
    assert awslogs.keep(plain, events_only=False, level="info") and awslogs.keep(error, level="ERROR")
    assert awslogs.keep(event, grep="TP.CSV".lower()) and awslogs.keep(event, grep="TP.CSV")
    assert not awslogs.keep(event, grep="nothing like it")


def test_spans_and_moments() -> None:
    assert awslogs.since("15m") == timedelta(minutes=15) and awslogs.since("2d") == timedelta(days=2)
    assert awslogs.since("30s") == timedelta(seconds=30) and awslogs.since(" 1 H ") == timedelta(hours=1)
    with pytest.raises(ValueError):
        awslogs.since("yesterday")
    today = datetime(2026, 11, 2, 15, 0).astimezone()
    assert awslogs.moment("09:30", today) == today.replace(hour=9, minute=30)
    assert awslogs.moment("2026-11-01 08:00", today) == datetime(2026, 11, 1, 8, 0, tzinfo=today.tzinfo)
    assert awslogs.moment("2026-11-01T08:00:05", today).second == 5
    assert awslogs.moment("2026-11-01", today).hour == 0
    with pytest.raises(ValueError):
        awslogs.moment("noon", today)


# --------------------------------------------------------------------------- asking AWS


class FakeLogs:
    """The AWS CLI's answers to filter-log-events (by log group) and describe-log-groups."""

    def __init__(self, groups: dict[str, list[dict]]) -> None:
        self.groups = groups
        self.calls: list[list[str]] = []
        self.denied: set[str] = set()
        self.expired = False
        self.next_token: dict[str, str] = {}

    def __call__(self, profile: str, args: list[str], timeout: float) -> str:
        self.calls.append(args)
        if self.expired:
            raise awsenv.AwsError("Error when retrieving token from sso: Token has expired and refresh failed",
                                  expired=True)
        if args[:2] == ["logs", "describe-log-groups"]:
            return json.dumps([awslogs.log_group(name) for name in self.groups] + ["/aws/apigateway/other"])
        assert args[:2] == ["logs", "filter-log-events"], args
        group = args[args.index("--log-group-name") + 1][len(awslogs.GROUP_PREFIX):]
        if group in self.denied:
            raise awsenv.AwsError("An error occurred (AccessDeniedException) when calling the FilterLogEvents "
                                  "operation: User is not authorized to perform: logs:FilterLogEvents")
        if group not in self.groups:
            raise awsenv.AwsError("An error occurred (ResourceNotFoundException): The specified log group does not exist.")
        start = int(args[args.index("--start-time") + 1])
        found = [e for e in self.groups[group] if e["timestamp"] >= start]
        answer = {"events": found, "searchedLogStreams": []}
        if group in self.next_token:
            answer["NextToken"] = self.next_token[group]
        return json.dumps(answer)


@pytest.fixture
def fake_logs(monkeypatch) -> FakeLogs:
    fake = FakeLogs({
        "broker-sqs-event": [cloudwatch(broker_received(EVENT), 2000, "b"), cloudwatch(sqs_send(EVENT), 2001, "b")],
        "account-publish-ev": [cloudwatch(sns_publish("TP_ACCOUNT_UPDATED", {"entity_id": "a-1"}), 1500, "a")],
    })
    monkeypatch.setattr(awsenv, "_aws", fake)
    monkeypatch.setattr(awsenv, "cli", lambda: "/usr/bin/aws")
    return fake


def test_read_asks_each_lambda_within_the_time_and_mixes_their_lines_in_order(fake_logs) -> None:
    result = awslogs.read("pdm-dev", ["broker-sqs-event", "account-publish-ev"], 1000, 3000, awslogs.pattern(), 50)
    assert [line.function for line in result.lines] == ["account-publish-ev", "broker-sqs-event", "broker-sqs-event"]
    assert result.errors == {} and result.more == {}
    call = next(c for c in fake_logs.calls if "broker-sqs-event" in c[3])
    assert call[call.index("--start-time") + 1] == "1000" and call[call.index("--end-time") + 1] == "3000"
    assert call[call.index("--max-items") + 1] == "50"
    assert call[call.index("--filter-pattern") + 1] == awslogs.pattern()


def test_a_lambda_that_cannot_be_read_does_not_stop_the_others(fake_logs) -> None:
    fake_logs.denied.add("account-publish-ev")
    fake_logs.next_token["broker-sqs-event"] = "tok"
    result = awslogs.read("pdm-dev", ["broker-sqs-event", "account-publish-ev", "gone"], 0)
    assert {line.function for line in result.lines} == {"broker-sqs-event"}
    assert result.errors["account-publish-ev"].kind == "denied"
    assert "logs:FilterLogEvents" in result.errors["account-publish-ev"].message
    assert result.errors["gone"].kind == "missing"
    assert result.more == {"broker-sqs-event": "tok"}
    call = next(c for c in fake_logs.calls if "broker-sqs-event" in c[3])
    assert "--end-time" not in call and "--starting-token" not in call
    awslogs.read("pdm-dev", ["broker-sqs-event"], 0, tokens={"broker-sqs-event": "tok"})
    assert fake_logs.calls[-1][-2:] == ["--starting-token", "tok"]


def test_a_session_that_is_over_stops_the_read(fake_logs) -> None:
    fake_logs.expired = True
    with pytest.raises(awslogs.LogsError) as caught:
        awslogs.read("pdm-dev", ["broker-sqs-event"], 0)
    assert caught.value.expired and caught.value.kind == "expired"


def test_following_reads_again_a_while_back_and_shows_each_line_once(fake_logs) -> None:
    seen = {"old": 10}
    first = awslogs.follow("pdm-dev", ["broker-sqs-event"], 2000 + awslogs.OVERLAP, seen)
    assert len(first.lines) == 2 and set(seen) == {line.id for line in first.lines}  # "old" is too old to come back
    fake_logs.groups["broker-sqs-event"].append(cloudwatch(sqs_send({"type": "late"}), 2500, "b"))
    second = awslogs.follow("pdm-dev", ["broker-sqs-event"], 2100 + awslogs.OVERLAP, seen)
    assert [line.messages[0].type for line in second.lines] == ["late"]
    starts = [int(c[c.index("--start-time") + 1]) for c in fake_logs.calls]
    assert starts == [2000, 2100]


def test_lambdas_are_the_log_groups_of_lambda(fake_logs) -> None:
    assert awslogs.lambdas("pdm-dev") == ["account-publish-ev", "broker-sqs-event"]


def test_suggestions_come_from_the_dev_terraform_and_the_event_map(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(awsenv, "terraform_names", lambda root: {
        "broker/broker-sqs-event": ["broker-sqs-event"], "lead/account-publish-ev": ["account-publish-ev"],
        "lead/lead-tp-export": ["lead-tp-export", "lead-tp-export-ev"], "other/x": ["x"],
    })
    monkeypatch.setattr(events, "load_event_map", lambda root: events.EventMap(
        consumers={"lead_tp_export.fifo": events.Consumer("lead/lead-tp-export", "main.handler"),
                   "broker.fifo": events.Consumer("broker/broker-sqs-event", "main.handler")},
        broker_queue="broker.fifo",
        topic_variables={"lead/account-publish-ev": {"SNS_ACCOUNT_ARN": "sns-account-publish.fifo"}},
    ))
    found = awslogs.suggestions(tmp_path)
    assert [(s.function, s.role, s.detail) for s in found] == [
        ("broker-sqs-event", "broker", "broker.fifo"),
        ("account-publish-ev", "sns", "sns-account-publish.fifo"),
        ("lead-tp-export", "consumer", "lead_tp_export.fifo"),
        ("lead-tp-export-ev", "consumer", "lead_tp_export.fifo"),
    ]
    assert awslogs.event_flow(tmp_path) == ["broker-sqs-event", "account-publish-ev"]


# --------------------------------------------------------------------------- pdms aws logs / lambdas


@pytest.fixture
def cfg(tmp_path, monkeypatch) -> Config:
    root = tmp_path / "pdms"
    root.mkdir()
    cfg = Config(defaults=Defaults(aws_profile="pdm-dev"), repos={"pdms": Repo(path=str(root))}, current_repo="pdms")
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(awslogs, "event_flow", lambda root: ["broker-sqs-event", "account-publish-ev"])
    monkeypatch.setattr(awslogs, "suggestions", lambda root: [awslogs.Suggestion("broker-sqs-event", "broker", "b.fifo")])
    monkeypatch.setenv("PDMS_NO_UPDATE_CHECK", "1")
    return cfg


def run(*args: str):
    return CliRunner().invoke(cli.app, ["aws", *args], env={"COLUMNS": "200"})


def test_pdms_aws_logs_shows_the_events_of_the_event_flow(fake_logs, cfg) -> None:
    result = run("logs", "--since", "100000d")
    assert result.exit_code == 0, result.output
    assert "Reading broker-sqs-event, account-publish-ev" in result.output
    assert "account-publish-ev → sns-account-publish.fifo · TP_ACCOUNT_UPDATED" in result.output
    assert "broker-sqs-event ← received · lead-tp-export" in result.output
    assert "broker-sqs-event → lead_tp_export-sqs-queue.fifo · lead-tp-export" in result.output
    assert '"file_name": "tp.csv"' in result.output
    short = run("logs", "broker-sqs-event", "--since", "100000d", "--short", "--type", "lead-tp-export")
    assert "file_name" not in short.output and short.output.count("lead-tp-export") == 2
    call = fake_logs.calls[-1]
    assert call[call.index("--filter-pattern") + 1] == '"lead-tp-export"'


def test_pdms_aws_logs_json_is_one_object_per_line(fake_logs, cfg) -> None:
    result = run("logs", "account-publish-ev", "--since", "100000d", "--json")
    [line] = [json.loads(text) for text in result.stdout.splitlines()]
    assert line["function"] == "account-publish-ev" and line["messages"][0]["type"] == "TP_ACCOUNT_UPDATED"
    assert line["time"] == awslogs.local_time(1500) and line["at"] == 1500


def test_pdms_aws_logs_says_what_it_could_not_read_and_when_nothing_was_found(fake_logs, cfg) -> None:
    fake_logs.denied.add("account-publish-ev")
    result = run("logs", "account-publish-ev", "--since", "1h")
    assert result.exit_code == 0
    assert "cannot read the logs of account-publish-ev" in result.output and "Nothing found from" in result.output


def test_pdms_aws_logs_fails_clearly(fake_logs, cfg) -> None:
    assert "not a span of time" in run("logs", "--since", "a while").output
    assert "not a date" in run("logs", "--from", "noon").output
    assert "--follow reads up to now" in run("logs", "--to", "09:00", "-f").output
    fake_logs.expired = True
    result = run("logs", "broker-sqs-event")
    assert result.exit_code == 1 and "The AWS session of pdm-dev is over: pdms aws login." in result.output
    cfg.defaults.aws_profile = ""
    assert "Choose an AWS profile first" in run("logs").output


def test_pdms_aws_lambdas_marks_those_that_take_part_in_events(fake_logs, cfg) -> None:
    result = run("lambdas")
    assert result.exit_code == 0, result.output
    words = [line.split() for line in result.output.splitlines()]
    assert ["account-publish-ev"] in words and ["broker-sqs-event", "broker", "b.fifo"] in words
    events_only = run("lambdas", "--events")
    assert "account-publish-ev" not in events_only.output and "broker-sqs-event" in events_only.output
    assert "No Lambda found." in run("lambdas", "nothing").output


def test_completion_offers_the_lambdas_of_the_dev_terraform(cfg, monkeypatch) -> None:
    from pdms_cli import completion

    monkeypatch.setattr(awsenv, "terraform_names", lambda root: {"a/b": ["broker-sqs-event"], "c/d": ["lead-x", "y"]})
    assert completion.lambdas("bro") == ["broker-sqs-event"]
    assert completion.lambdas("") == ["broker-sqs-event", "lead-x", "y"]


def test_the_package_never_evaluates_code_from_a_line() -> None:
    hostile = botocore("SendMessage", "__import__('os').system('echo hi')")
    assert parsed(hostile).messages == []
    assert awslogs._literal_at("{'a': __import__('os')}", 0) is None
    assert Path(awslogs.__file__).read_text(encoding="utf-8").count("eval(") == 1  # ast.literal_eval only


# --------------------------------------------------------------------------- pdms ui: the AWS logs screen


def test_ui_reads_the_lines_kept_by_the_filters(fake_logs, cfg) -> None:
    found = ui_awslogs.read(cfg, "broker-sqs-event,account-publish-ev", "0")
    assert [line["function"] for line in found["lines"]] == ["account-publish-ev", "broker-sqs-event", "broker-sqs-event"]
    assert found["lines"][0]["messages"][0]["type"] == "TP_ACCOUNT_UPDATED" and found["lines"][0]["time"]
    assert found["profile"] == "pdm-dev" and found["errors"] == {} and found["asked"] > 0
    only = ui_awslogs.read(cfg, "broker-sqs-event", "0", event_type="lead-tp-export", limit="5")
    assert len(only["lines"]) == 2
    call = fake_logs.calls[-1]
    assert call[call.index("--filter-pattern") + 1] == '"lead-tp-export"' and call[call.index("--max-items") + 1] == "5"


def test_ui_read_checks_what_the_page_sends(fake_logs, cfg) -> None:
    for functions, start in (("", "0"), ("--profile x", "0"), ("a;b", "0"), ("broker-sqs-event", ""),
                             ("broker-sqs-event", "yesterday")):
        with pytest.raises(actions.ActionError):
            ui_awslogs.read(cfg, functions, start)
    assert fake_logs.calls == []


def test_ui_says_when_the_session_is_over_or_no_profile_is_chosen(fake_logs, cfg) -> None:
    fake_logs.expired = True
    over = ui_awslogs.read(cfg, "broker-sqs-event", "0")
    assert over["expired"] and over["lines"] == []
    cfg.defaults.aws_profile = ""
    assert ui_awslogs.read(cfg, "broker-sqs-event", "0")["profile"] == ""
    assert ui_awslogs.lambdas(cfg) == {"profile": "", "lambdas": [], "flow": []}


def test_ui_lists_the_lambdas_once_in_a_while(fake_logs, cfg, monkeypatch) -> None:
    monkeypatch.setattr(ui_awslogs, "_listed", {})
    monkeypatch.setattr(awslogs, "suggestions", lambda root: [awslogs.Suggestion("broker-sqs-event", "broker", "b.fifo"),
                                                              awslogs.Suggestion("not-deployed", "sns", "t")])
    found = ui_awslogs.lambdas(cfg)
    assert found["lambdas"] == [{"name": "account-publish-ev", "role": "", "detail": ""},
                                {"name": "broker-sqs-event", "role": "broker", "detail": "b.fifo"}]
    assert found["flow"] == ["broker-sqs-event"]  # not-deployed has no logs
    ui_awslogs.lambdas(cfg)
    assert sum(c[:2] == ["logs", "describe-log-groups"] for c in fake_logs.calls) == 1
    ui_awslogs.lambdas(cfg, fresh=True)
    assert sum(c[:2] == ["logs", "describe-log-groups"] for c in fake_logs.calls) == 2
    fake_logs.expired = True
    monkeypatch.setattr(ui_awslogs, "_listed", {})
    over = ui_awslogs.lambdas(cfg)
    assert over["expired"] and [item["name"] for item in over["lambdas"]] == ["broker-sqs-event", "not-deployed"]


def test_the_api_routes_the_screen(fake_logs, cfg) -> None:
    api = Api(None, None, None)
    status, data = api.get("/api/aws-logs/read", {"functions": ["broker-sqs-event"], "start": ["0"], "all": ["1"]})
    assert status == 200 and len(data["lines"]) == 2
    assert api.get("/api/aws-logs/read", {"functions": ["x y"], "start": ["0"]})[0] == 400
    assert api.get("/api/aws-logs/lambdas", {})[0] == 200
