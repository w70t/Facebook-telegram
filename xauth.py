"""تسجيل دخول X عبر Twikit مع تحديات غير متزامنة وآمنة للروبوت.

Twikit 2.3.3 يقرأ رموز التحقق بواسطة ``input()`` داخل ``Client.login``.
ذلك لا يعمل تحت systemd ويوقف حلقة Telegram. هذا المحوّل يعيد نفس تدفق
onboarding، لكنه يسلّم كل تحدٍّ إلى callback غير متزامن توفره واجهة Telegram.

الواجهة تعتمد عمداً على internals في Twikit 2.3.3؛ لذلك الإصدار مثبت بدقة في
requirements.txt ونرفض أي إصدار آخر بدلاً من متابعة تدفق دخول قد تغيّر بصمت.

تدفق onboarding والـsubtask versions مشتقان من Twikit 2.3.3 (MIT).
راجع THIRD_PARTY_NOTICES.md.
"""
from importlib.metadata import PackageNotFoundError, version


SUPPORTED_TWIKIT = "2.3.3"
MAX_CHALLENGE_STEPS = 5


class XAuthError(RuntimeError):
    """الأساس لأخطاء محوّل تسجيل X."""


class XAuthCompatibilityError(XAuthError):
    """إصدار Twikit أو واجهته الداخلية غير مدعومين."""


class XCredentialsRejected(XAuthError):
    """رفض X اسم المستخدم/البريد/كلمة المرور."""


class XChallengeRejected(XAuthError):
    """رفض X رمز تحقق مؤقت؛ لا يعني أن الحساب محظور."""


class XUnsupportedChallenge(XAuthError):
    """طلب X نوع تحقق لا تدعمه واجهة البوت."""


class XChallengeResponseError(XAuthError):
    """لم توفر الواجهة رمزاً صالحاً للتحدي."""


SUBTASK_VERSIONS = {
    "action_list": 2,
    "alert_dialog": 1,
    "app_download_cta": 1,
    "check_logged_in_account": 1,
    "choice_selection": 3,
    "contacts_live_sync_permission_prompt": 0,
    "cta": 7,
    "email_verification": 2,
    "end_flow": 1,
    "enter_date": 1,
    "enter_email": 2,
    "enter_password": 5,
    "enter_phone": 2,
    "enter_recaptcha": 1,
    "enter_text": 5,
    "enter_username": 2,
    "generic_urt": 3,
    "in_app_notification": 1,
    "interest_picker": 3,
    "js_instrumentation": 1,
    "menu_dialog": 1,
    "notifications_permission_prompt": 2,
    "open_account": 2,
    "open_home_timeline": 1,
    "open_link": 1,
    "phone_verification": 4,
    "privacy_options": 1,
    "security_key": 3,
    "select_avatar": 4,
    "select_banner": 2,
    "settings_list": 7,
    "show_code": 1,
    "sign_up": 2,
    "sign_up_review": 4,
    "tweet_selection_urt": 1,
    "update_users": 1,
    "upload_media": 1,
    "user_recommendations_list": 4,
    "user_recommendations_urt": 1,
    "wait_spinner": 3,
    "web_modal": 1,
}


def _require_supported_twikit():
    try:
        installed = version("twikit")
    except PackageNotFoundError as exc:
        raise XAuthCompatibilityError("Twikit غير مثبت") from exc
    if installed != SUPPORTED_TWIKIT:
        raise XAuthCompatibilityError(
            f"إصدار Twikit غير مدعوم: {installed}; المطلوب {SUPPORTED_TWIKIT}"
        )
    return installed


def _load_twikit_api():
    _require_supported_twikit()
    try:
        from twikit.client.client import Flow, find_dict, solve_ui_metrics
    except (ImportError, AttributeError) as exc:
        raise XAuthCompatibilityError("واجهة Twikit الداخلية تغيّرت") from exc
    return Flow, find_dict, solve_ui_metrics


def _require_client_api(client):
    required = ("http", "v11", "_get_guest_token", "_ui_metrics")
    if any(not hasattr(client, name) for name in required):
        raise XAuthCompatibilityError("عميل Twikit لا يطابق الواجهة المدعومة")
    cookies = getattr(client.http, "cookies", None)
    if cookies is None or not callable(getattr(cookies, "clear", None)):
        raise XAuthCompatibilityError("مخزن cookies في Twikit غير متوافق")


async def _challenge_response(handler, kind, prompt=""):
    value = await handler(kind, prompt)
    if not isinstance(value, str) or not value.strip():
        raise XChallengeResponseError("لم يصل رمز تحقق صالح")
    return value.strip()


def _deny(stage):
    if stage == "credentials":
        # هذه العبارة مقصودة أيضاً كي يصنّف XReader الرفض كخطأ اعتماد قطعي.
        raise XCredentialsRejected("invalid credentials")
    raise XChallengeRejected("verification code rejected")


def _expect_task(flow, *expected):
    """يفشل مغلقاً قبل إرسال بيانات إلى خطوة لم يطلبها X صراحة."""
    if flow.task_id not in expected:
        task_id = flow.task_id or "<none>"
        raise XUnsupportedChallenge(f"تحدي X غير مدعوم: {task_id}")


async def login_with_challenges(
    client,
    *,
    auth_info_1,
    auth_info_2,
    password,
    challenge_handler,
    enable_ui_metrics=True,
):
    """يسجل الدخول ويطلب رموز X عبر ``challenge_handler`` دون أي ``input``.

    ``challenge_handler(kind, prompt)`` دالة async، و``kind`` إما
    ``verification`` لتأكيد إضافي أو ``two_factor`` لرمز Authenticator.
    لا تحفظ هذه الدالة كلمة المرور أو الرموز ولا تكتب cookies.
    """
    if not callable(challenge_handler):
        raise TypeError("challenge_handler يجب أن يكون callable")
    if not auth_info_1 or not password:
        raise ValueError("اسم المستخدم وكلمة المرور مطلوبان")

    Flow, find_dict, solve_ui_metrics = _load_twikit_api()
    _require_client_api(client)
    client.http.cookies.clear()

    guest_token = await client._get_guest_token()
    flow = Flow(client, guest_token)
    await flow.execute_task(
        params={"flow_name": "login"},
        data={
            "input_flow_data": {
                "flow_context": {
                    "debug_overrides": {},
                    "start_location": {"location": "splash_screen"},
                }
            },
            "subtask_versions": SUBTASK_VERSIONS,
        },
    )
    await flow.sso_init("apple")

    _expect_task(flow, "LoginJsInstrumentationSubtask")
    if enable_ui_metrics:
        metrics = solve_ui_metrics(await client._ui_metrics())
    else:
        metrics = ""
    await flow.execute_task({
        "subtask_id": "LoginJsInstrumentationSubtask",
        "js_instrumentation": {"response": metrics, "link": "next_link"},
    })
    _expect_task(flow, "LoginEnterUserIdentifierSSO")
    await flow.execute_task({
        "subtask_id": "LoginEnterUserIdentifierSSO",
        "settings_list": {
            "setting_responses": [{
                "key": "user_identifier",
                "response_data": {"text_data": {"result": auth_info_1}},
            }],
            "link": "next_link",
        },
    })

    if flow.task_id == "LoginEnterAlternateIdentifierSubtask":
        alternate_identifier = auth_info_2
        if not alternate_identifier:
            # Twikit الأصلي يلجأ إلى input() هنا إذا لم يمرر المستدعي البريد أو
            # الهاتف. نوجّه الطلب إلى Telegram كي يبقى البوت غير تفاعلي مع stdin.
            alternate_identifier = await _challenge_response(
                challenge_handler, "verification", "alternate_identifier"
            )
        alternate_payload = {
            "subtask_id": "LoginEnterAlternateIdentifierSubtask",
            "enter_text": {"text": alternate_identifier, "link": "next_link"},
        }
        try:
            await flow.execute_task(alternate_payload)
        finally:
            alternate_payload["enter_text"]["text"] = None
            alternate_identifier = None
    if flow.task_id == "DenyLoginSubtask":
        _deny("credentials")

    _expect_task(flow, "LoginEnterPassword")
    password_payload = {
        "subtask_id": "LoginEnterPassword",
        "enter_password": {"password": password, "link": "next_link"},
    }
    try:
        await flow.execute_task(password_payload)
    finally:
        # لا نُبقي نسخة إضافية من كلمة المرور في قاموس الطلب بعد اكتمال await.
        password_payload["enter_password"]["password"] = None
        password = None
    if flow.task_id == "DenyLoginSubtask":
        _deny("credentials")

    if flow.task_id not in (
        None,
        "LoginAcid",
        "LoginTwoFactorAuthChallenge",
        "AccountDuplicationCheck",
    ):
        _expect_task(
            flow,
            "LoginAcid",
            "LoginTwoFactorAuthChallenge",
            "AccountDuplicationCheck",
        )

    # قد يأتي LoginAcid ثم 2FA، وقد يعيد X نفس تحدي 2FA بعد رمز خاطئ.
    # الحلقة المحدودة تمنع تدفقاً متغيراً من إبقاء المحاولة مفتوحة إلى الأبد.
    challenge_steps = 0
    while flow.task_id in ("LoginAcid", "LoginTwoFactorAuthChallenge"):
        challenge_steps += 1
        if challenge_steps > MAX_CHALLENGE_STEPS:
            raise XChallengeRejected("too many verification attempts")
        task_id = flow.task_id
        kind = "two_factor" if task_id == "LoginTwoFactorAuthChallenge" else "verification"
        response = await _challenge_response(challenge_handler, kind)
        await flow.execute_task({
            "subtask_id": task_id,
            "enter_text": {"text": response, "link": "next_link"},
        })
        # أزل المرجع في أسرع وقت؛ لا نعيد استخدام رمز لمرة واحدة.
        response = None
        if flow.task_id == "DenyLoginSubtask":
            _deny("challenge")

    if flow.task_id not in (None, "AccountDuplicationCheck"):
        _expect_task(flow, "AccountDuplicationCheck")

    if flow.task_id == "AccountDuplicationCheck":
        await flow.execute_task({
            "subtask_id": "AccountDuplicationCheck",
            "check_logged_in_account": {"link": "AccountDuplicationCheck_false"},
        })
        if flow.task_id == "DenyLoginSubtask":
            _deny("challenge")
        if flow.task_id not in (None, "OpenAccount", "OpenHomeTimeline"):
            _expect_task(flow, "OpenAccount", "OpenHomeTimeline")

    response = flow.response
    if response and response.get("subtasks"):
        try:
            ids = find_dict(response, "id_str", find_one=True)
            if ids:
                client._user_id = ids[0]
        except (KeyError, IndexError, TypeError):
            pass
    return response
