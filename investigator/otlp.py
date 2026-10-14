"""OTLP/HTTP trace decoding through official protobuf schemas; no custom wire codec."""

import base64
import gzip
import io
import json
import re
import zlib
from datetime import datetime, timezone

from google.protobuf import json_format
from google.protobuf.message import DecodeError
from google.rpc.status_pb2 import Status
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest, ExportTraceServiceResponse

from investigator.analysis import redact

MAX_BODY = 4 * 1024 * 1024


def _json_message(data, descriptor, depth=0):
    if depth > 30 or not isinstance(data, dict):
        raise ValueError("Invalid or overly nested OTLP JSON.")
    known = {f.json_name: f for f in descriptor.fields}
    result = {}
    for name, value in data.items():
        field = known.get(name)
        if field is None:
            continue  # OTLP requires unknown message fields to be ignored.
        if value is None:
            continue
        values = value if field.is_repeated else [value]
        if not isinstance(values, list):
            raise ValueError("Invalid repeated field.")
        converted = []
        for item in values:
            if field.message_type:
                item = _json_message(item, field.message_type, depth + 1)
            elif field.enum_type and type(item) is not int:
                raise ValueError("OTLP enum values must be integers.")
            elif field.name in {"trace_id", "span_id", "parent_span_id"}:
                if not isinstance(item, str) or not re.fullmatch(r"(?:[0-9a-fA-F]{2})*", item):
                    raise ValueError("OTLP IDs must be hex strings.")
                try:
                    item = base64.b64encode(bytes.fromhex(item)).decode()
                except ValueError:
                    raise ValueError("Invalid OTLP hex ID.") from None
            converted.append(item)
        result[name] = converted if field.is_repeated else converted[0]
    return result


def decode(body, content_type, encoding=None):
    if len(body) > MAX_BODY:
        raise OverflowError("OTLP body exceeds 4 MiB.")
    if encoding == "gzip":
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(body)) as stream:
                body = stream.read(MAX_BODY + 1)
        except (OSError, EOFError, zlib.error):
            raise ValueError("Invalid gzip payload.") from None
        if len(body) > MAX_BODY:
            raise OverflowError("Expanded OTLP body exceeds 4 MiB.")
    elif encoding not in (None, "identity"):
        raise ValueError("Only identity and gzip content encoding are supported.")
    message = ExportTraceServiceRequest()
    try:
        if content_type == "application/x-protobuf":
            message.ParseFromString(body)
        elif content_type == "application/json":
            data = _json_message(json.loads(body), message.DESCRIPTOR)
            json_format.ParseDict(data, message, ignore_unknown_fields=True)
        else:
            raise ValueError("Use application/json or application/x-protobuf.")
    except (DecodeError, json_format.ParseError, UnicodeDecodeError, RecursionError, TypeError, IndexError):
        raise ValueError("Malformed OTLP trace export.") from None
    return message


def _attributes(items):
    result = {}
    for item in items:
        kind = item.value.WhichOneof("value")
        if kind in {"string_value", "int_value", "bool_value", "double_value"}:
            result[item.key] = getattr(item.value, kind)
    return result


def _time(nanoseconds):
    seconds, fraction = divmod(nanoseconds, 10 ** 9)
    return datetime.fromtimestamp(seconds, timezone.utc).replace(microsecond=fraction // 1000).isoformat().replace("+00:00", "Z")


def spans(message):
    rows, rejected = [], 0
    for resource in message.resource_spans:
        attributes = _attributes(resource.resource.attributes)
        service = attributes.get("service.name")
        env = attributes.get("deployment.environment.name", attributes.get("deployment.environment", "development"))
        for scope in resource.scope_spans:
            for span in scope.spans:
                if len(rows) + rejected >= 2000:
                    raise OverflowError("Export exceeds 2,000 spans.")
                try:
                    if (not isinstance(service, str) or not service.strip() or len(service) > 120 or not isinstance(env, str)
                            or len(span.trace_id) != 16 or not any(span.trace_id) or len(span.span_id) != 8 or not any(span.span_id)
                            or (span.parent_span_id and (len(span.parent_span_id) != 8 or not any(span.parent_span_id)))
                            or span.end_time_unix_nano < span.start_time_unix_nano or not span.start_time_unix_nano):
                        raise ValueError()
                    attrs = _attributes(span.attributes)
                    status = attrs.get("http.response.status_code", attrs.get("http.status_code"))
                    status = status if type(status) is int and 100 <= status <= 599 else None
                    failed = span.status.code == 2 or (status is not None and status >= 500)
                    rows.append(redact({"request_id": span.trace_id.hex(), "span_id": span.span_id.hex(), "parent_span_id": span.parent_span_id.hex(),
                                       "service": service.strip(), "environment": env, "start_time": _time(span.start_time_unix_nano),
                                       "timestamp": _time(span.end_time_unix_nano), "duration_ms": (span.end_time_unix_nano - span.start_time_unix_nano) / 10 ** 6,
                                       "level": "ERROR" if failed else "INFO", "status": status,
                                       "message": (span.name + (" / " + span.status.message if span.status.message else ""))[:2000],
                                       "span_kind": "client" if span.kind == 3 else "server" if span.kind == 2 else "internal",
                                       "dependency": str(attrs.get("peer.service", ""))[:120], "origin": "otlp",
                                       "events": [{"name": e.name[:120], "timestamp": _time(e.time_unix_nano)} for e in list(span.events)[:30]]}))
                except (ValueError, OverflowError, OSError):
                    rejected += 1
    return rows, rejected


def response(content_type, rejected=0, error=None, code=3):
    if error is not None:
        message = Status(code=code, message=error)
    else:
        message = ExportTraceServiceResponse()
        if rejected:
            message.partial_success.rejected_spans = rejected
            message.partial_success.error_message = "Invalid, unregistered, inactive or capacity-limited spans were rejected."
    if content_type == "application/x-protobuf":
        return message.SerializeToString(), content_type
    return json_format.MessageToJson(message, preserving_proto_field_name=False, use_integers_for_enums=True).encode(), "application/json"
