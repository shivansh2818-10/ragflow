#
#  Copyright 2024 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
import json
from copy import deepcopy
from uuid import uuid4

from flask import request, Response

from api.db import StatusEnum
from api.db.services.dialog_service import DialogService, ConversationService, chat
from api.utils import get_uuid
from api.utils.api_utils import get_data_error_result
from api.utils.api_utils import get_json_result, token_required


@manager.route('/save', methods=['POST'])
@token_required
def set_conversation(tenant_id):
    req = request.json
    conv_id = req.get("id")
    if "messages" in req:
        req["message"] = req.pop("messages")
        if req["message"]:
            for message in req["message"]:
                if "reference" in message:
                    req["reference"] = message.pop("reference")
    if "assistant_id" in req:
        req["dialog_id"] = req.pop("assistant_id")
    if "id" in req:
        del req["id"]
        conv = ConversationService.query(id=conv_id)
        if not conv:
            return get_data_error_result(retmsg="Session does not exist")
        if not DialogService.query(id=conv[0].dialog_id, tenant_id=tenant_id, status=StatusEnum.VALID.value):
            return get_data_error_result(retmsg="You do not own the session")
        if req.get("dialog_id"):
            dia = DialogService.query(tenant_id=tenant_id, id=req["dialog_id"], status=StatusEnum.VALID.value)
            if not dia:
                return get_data_error_result(retmsg="You do not own the assistant")
        if "dialog_id" in req and not req.get("dialog_id"):
            return get_data_error_result(retmsg="assistant_id can not be empty.")
        if "name" in req and not req.get("name"):
            return get_data_error_result(retmsg="name can not be empty.")
        if "message" in req and not req.get("message"):
            return get_data_error_result(retmsg="messages can not be empty")
        if not ConversationService.update_by_id(conv_id, req):
            return get_data_error_result(retmsg="Session updates error")
        return get_json_result(data=True)

    if not req.get("dialog_id"):
        return get_data_error_result(retmsg="assistant_id is required.")
    dia = DialogService.query(tenant_id=tenant_id, id=req["dialog_id"], status=StatusEnum.VALID.value)
    if not dia:
        return get_data_error_result(retmsg="You do not own the assistant")
    conv = {
        "id": get_uuid(),
        "dialog_id": req["dialog_id"],
        "name": req.get("name", "New session"),
        "message": req.get("message", [{"role": "assistant", "content": dia[0].prompt_config["prologue"]}]),
        "reference": req.get("reference", [])
    }
    if not conv.get("name"):
        return get_data_error_result(retmsg="name can not be empty.")
    if not conv.get("message"):
        return get_data_error_result(retmsg="messages can not be empty")
    ConversationService.save(**conv)
    e, conv = ConversationService.get_by_id(conv["id"])
    if not e:
        return get_data_error_result(retmsg="Fail to new session!")
    conv = conv.to_dict()
    conv["messages"] = conv.pop("message")
    conv["assistant_id"] = conv.pop("dialog_id")
    for message in conv["messages"]:
        message["reference"] = conv.get("reference")
    del conv["reference"]
    return get_json_result(data=conv)


@manager.route('/completion', methods=['POST'])
@token_required
def completion(tenant_id):
    req = request.json
    # req = {"conversation_id": "9aaaca4c11d311efa461fa163e197198", "messages": [
    #    {"role": "user", "content": "上海有吗？"}
    # ]}
    msg = []
    question = {
        "content": req.get("question"),
        "role": "user",
        "id": str(uuid4())
    }
    req["messages"].append(question)
    for m in req["messages"]:
        if m["role"] == "system": continue
        if m["role"] == "assistant" and not msg: continue
        m["id"] = m.get("id", str(uuid4()))
        msg.append(m)
    message_id = msg[-1].get("id")
    conv = ConversationService.query(id=req["id"])
    conv = conv[0]
    if not conv:
        return get_data_error_result(retmsg="Session does not exist")
    if not DialogService.query(id=conv.dialog_id, tenant_id=tenant_id, status=StatusEnum.VALID.value):
        return get_data_error_result(retmsg="You do not own the session")
    conv.message = deepcopy(req["messages"])
    e, dia = DialogService.get_by_id(conv.dialog_id)
    if not e:
        return get_data_error_result(retmsg="Dialog not found!")
    del req["id"]
    del req["messages"]

    if not conv.reference:
        conv.reference = []
    conv.message.append({"role": "assistant", "content": "", "id": message_id})
    conv.reference.append({"chunks": [], "doc_aggs": []})

    def fillin_conv(ans):
        nonlocal conv, message_id
        if not conv.reference:
            conv.reference.append(ans["reference"])
        else:
            conv.reference[-1] = ans["reference"]
        conv.message[-1] = {"role": "assistant", "content": ans["answer"],
                            "id": message_id, "prompt": ans.get("prompt", "")}
        ans["id"] = message_id

    def stream():
        nonlocal dia, msg, req, conv
        try:
            for ans in chat(dia, msg, **req):
                fillin_conv(ans)
                yield "data:" + json.dumps({"retcode": 0, "retmsg": "", "data": ans}, ensure_ascii=False) + "\n\n"
            ConversationService.update_by_id(conv.id, conv.to_dict())
        except Exception as e:
            yield "data:" + json.dumps({"retcode": 500, "retmsg": str(e),
                                        "data": {"answer": "**ERROR**: " + str(e), "reference": []}},
                                       ensure_ascii=False) + "\n\n"
        yield "data:" + json.dumps({"retcode": 0, "retmsg": "", "data": True}, ensure_ascii=False) + "\n\n"

    if req.get("stream", True):
        resp = Response(stream(), mimetype="text/event-stream")
        resp.headers.add_header("Cache-control", "no-cache")
        resp.headers.add_header("Connection", "keep-alive")
        resp.headers.add_header("X-Accel-Buffering", "no")
        resp.headers.add_header("Content-Type", "text/event-stream; charset=utf-8")
        return resp

    else:
        answer = None
        for ans in chat(dia, msg, **req):
            answer = ans
            fillin_conv(ans)
            ConversationService.update_by_id(conv.id, conv.to_dict())
            break
        return get_json_result(data=answer)
