"""Pydantic models for Google Workspace Add-on request/response schemas.

Request models use extra="allow" so Google can add new fields without breaking us.
Response models use by_alias=True serialization to output the camelCase keys Google expects.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Request models (Google → our backend)
# ---------------------------------------------------------------------------


class TimeZone(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    id: str | None = None
    offset: int | None = None


class CommonEventObject(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    user_locale: str | None = Field(default=None, alias="userLocale")
    host_app: str | None = Field(default=None, alias="hostApp")
    platform: str | None = Field(default=None, alias="platform")
    time_zone: TimeZone | None = Field(default=None, alias="timeZone")
    invoked_function: str | None = Field(default=None, alias="invokedFunction")
    parameters: dict[str, str] | None = None
    form_inputs: dict | None = Field(default=None, alias="formInputs")


class AuthorizationEventObject(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    user_oauth_token: str | None = Field(default=None, alias="userOAuthToken")
    user_id_token: str | None = Field(default=None, alias="userIdToken")
    system_id_token: str | None = Field(default=None, alias="systemIdToken")


class GmailContext(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    message_id: str | None = Field(default=None, alias="messageId")
    thread_id: str | None = Field(default=None, alias="threadId")
    access_token: str | None = Field(default=None, alias="accessToken")


class AddonRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    common_event_object: CommonEventObject | None = Field(default=None, alias="commonEventObject")
    authorization_event_object: AuthorizationEventObject | None = Field(
        default=None, alias="authorizationEventObject"
    )
    gmail: GmailContext | None = None


# ---------------------------------------------------------------------------
# Response models (our backend → Google)
# ---------------------------------------------------------------------------


class TextParagraph(BaseModel):
    text: str


class DecoratedText(BaseModel):
    model_config = ConfigDict(populate_by_name=True, ser_json_by_alias=True)

    top_label: str | None = Field(default=None, alias="topLabel")
    text: str
    wrap_text: bool | None = Field(default=None, alias="wrapText")
    on_click: OnClick | None = Field(default=None, alias="onClick")


class Image(BaseModel):
    model_config = ConfigDict(populate_by_name=True, ser_json_by_alias=True)

    image_url: str = Field(alias="imageUrl")
    alt_text: str | None = Field(default=None, alias="altText")


class TextParagraphWidget(BaseModel):
    model_config = ConfigDict(populate_by_name=True, ser_json_by_alias=True)

    text_paragraph: TextParagraph = Field(alias="textParagraph")


class DecoratedTextWidget(BaseModel):
    model_config = ConfigDict(populate_by_name=True, ser_json_by_alias=True)

    decorated_text: DecoratedText = Field(alias="decoratedText")


class ImageWidget(BaseModel):
    model_config = ConfigDict(populate_by_name=True, ser_json_by_alias=True)

    image: Image


# ---------------------------------------------------------------------------
# Interactive widgets (buttons, inputs)
# ---------------------------------------------------------------------------


class ActionParameter(BaseModel):
    model_config = ConfigDict(populate_by_name=True, ser_json_by_alias=True)

    key: str
    value: str


class OnClickAction(BaseModel):
    model_config = ConfigDict(populate_by_name=True, ser_json_by_alias=True)

    function: str
    parameters: list[ActionParameter] | None = None
    required_widgets: list[str] | None = Field(default=None, alias="requiredWidgets")
    all_widgets_are_required: bool | None = Field(default=None, alias="allWidgetsAreRequired")


class OpenLink(BaseModel):
    model_config = ConfigDict(populate_by_name=True, ser_json_by_alias=True)

    url: str
    open_as: str | None = Field(default=None, alias="openAs")  # "FULL_SIZE" or "OVERLAY"
    on_close: str | None = Field(default=None, alias="onClose")  # "NOTHING" or "RELOAD"


class OnClick(BaseModel):
    model_config = ConfigDict(populate_by_name=True, ser_json_by_alias=True)

    action: OnClickAction | None = None
    open_link: OpenLink | None = Field(default=None, alias="openLink")


class Button(BaseModel):
    model_config = ConfigDict(populate_by_name=True, ser_json_by_alias=True)

    text: str
    on_click: OnClick = Field(alias="onClick")
    color: dict[str, float] | None = None  # {"red": 0.0, "green": 0.0, "blue": 0.0}
    disabled: bool | None = None


class ButtonList(BaseModel):
    model_config = ConfigDict(populate_by_name=True, ser_json_by_alias=True)

    buttons: list[Button]


class ButtonListWidget(BaseModel):
    model_config = ConfigDict(populate_by_name=True, ser_json_by_alias=True)

    button_list: ButtonList = Field(alias="buttonList")


class TextInput(BaseModel):
    model_config = ConfigDict(populate_by_name=True, ser_json_by_alias=True)

    name: str
    label: str
    type: str | None = None  # "SINGLE_LINE" or "MULTIPLE_LINE"
    value: str | None = None
    hint_text: str | None = Field(default=None, alias="hintText")


class TextInputWidget(BaseModel):
    model_config = ConfigDict(populate_by_name=True, ser_json_by_alias=True)

    text_input: TextInput = Field(alias="textInput")


class SelectionItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True, ser_json_by_alias=True)

    text: str
    value: str
    selected: bool | None = None


class SelectionInput(BaseModel):
    model_config = ConfigDict(populate_by_name=True, ser_json_by_alias=True)

    name: str
    label: str
    type: str | None = None  # "DROPDOWN", "RADIO_BUTTON", "CHECK_BOX", "SWITCH"
    items: list[SelectionItem]


class SelectionInputWidget(BaseModel):
    model_config = ConfigDict(populate_by_name=True, ser_json_by_alias=True)

    selection_input: SelectionInput = Field(alias="selectionInput")


class Divider(BaseModel):
    pass


class DividerWidget(BaseModel):
    model_config = ConfigDict(populate_by_name=True, ser_json_by_alias=True)

    divider: Divider = Field(default_factory=Divider)


Widget = (
    TextParagraphWidget
    | DecoratedTextWidget
    | ImageWidget
    | ButtonListWidget
    | TextInputWidget
    | SelectionInputWidget
    | DividerWidget
)


class Section(BaseModel):
    model_config = ConfigDict(populate_by_name=True, ser_json_by_alias=True)

    header: str | None = None
    widgets: list[Widget]
    collapsible: bool | None = None
    uncollapsible_widgets_count: int | None = Field(default=None, alias="uncollapsibleWidgetsCount")


class CardHeader(BaseModel):
    model_config = ConfigDict(populate_by_name=True, ser_json_by_alias=True)

    title: str
    subtitle: str | None = None
    image_url: str | None = Field(default=None, alias="imageUrl")
    image_type: str | None = Field(default=None, alias="imageType")


class Card(BaseModel):
    model_config = ConfigDict(populate_by_name=True, ser_json_by_alias=True)

    header: CardHeader | None = None
    sections: list[Section]


class PushCard(BaseModel):
    model_config = ConfigDict(populate_by_name=True, ser_json_by_alias=True)

    push_card: Card = Field(alias="pushCard")


class UpdateCard(BaseModel):
    model_config = ConfigDict(populate_by_name=True, ser_json_by_alias=True)

    update_card: Card = Field(alias="updateCard")


class ActionResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True, ser_json_by_alias=True)

    navigations: list[PushCard | UpdateCard]


class CardResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True, ser_json_by_alias=True)

    action: ActionResponse
