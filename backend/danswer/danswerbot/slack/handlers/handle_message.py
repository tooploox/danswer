import functools
import logging
from collections.abc import Callable
from typing import Any
from typing import cast
from typing import Optional
from typing import TypeVar

from retry import retry
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from sqlalchemy.orm import Session

from danswer.chat.chat_utils import compute_max_document_tokens
from danswer.configs.danswerbot_configs import DANSWER_BOT_ANSWER_GENERATION_TIMEOUT
from danswer.configs.danswerbot_configs import DANSWER_BOT_DISABLE_COT
from danswer.configs.danswerbot_configs import DANSWER_BOT_DISABLE_DOCS_ONLY_ANSWER
from danswer.configs.danswerbot_configs import DANSWER_BOT_DISPLAY_ERROR_MSGS
from danswer.configs.danswerbot_configs import DANSWER_BOT_NUM_RETRIES
from danswer.configs.danswerbot_configs import DANSWER_BOT_TARGET_CHUNK_PERCENTAGE
from danswer.configs.danswerbot_configs import DANSWER_REACT_EMOJI
from danswer.configs.danswerbot_configs import DISABLE_DANSWER_BOT_FILTER_DETECT
from danswer.configs.danswerbot_configs import ENABLE_DANSWERBOT_REFLEXION
from danswer.configs.model_configs import GEN_AI_MODEL_VERSION
from danswer.danswerbot.slack.blocks import build_documents_blocks
from danswer.danswerbot.slack.blocks import build_follow_up_block
from danswer.danswerbot.slack.blocks import build_qa_response_blocks
from danswer.danswerbot.slack.blocks import get_restate_blocks
from danswer.danswerbot.slack.constants import SLACK_CHANNEL_ID
from danswer.danswerbot.slack.models import SlackMessageInfo
from danswer.danswerbot.slack.utils import ChannelIdAdapter
from danswer.danswerbot.slack.utils import fetch_userids_from_emails
from danswer.danswerbot.slack.utils import respond_in_thread
from danswer.danswerbot.slack.utils import slack_usage_report
from danswer.danswerbot.slack.utils import SlackRateLimiter
from danswer.danswerbot.slack.utils import update_emote_react
from danswer.db.engine import get_sqlalchemy_engine
from danswer.db.models import SlackBotConfig
from danswer.llm.utils import check_number_of_tokens
from danswer.llm.utils import get_max_input_tokens
from danswer.one_shot_answer.answer_question import get_search_answer
from danswer.one_shot_answer.models import DirectQARequest
from danswer.one_shot_answer.models import OneShotQAResponse
from danswer.search.models import BaseFilters
from danswer.search.models import OptionalSearchSetting
from danswer.search.models import RetrievalDetails
from danswer.utils.logger import setup_logger

logger_base = setup_logger()

srl = SlackRateLimiter()

RT = TypeVar("RT")  # return type


def rate_limits(
    client: WebClient, channel: str, thread_ts: Optional[str]
) -> Callable[[Callable[..., RT]], Callable[..., RT]]:
    def decorator(func: Callable[..., RT]) -> Callable[..., RT]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> RT:
            if not srl.is_available():
                func_randid, position = srl.init_waiter()
                srl.notify(client, channel, position, thread_ts)
                while not srl.is_available():
                    srl.waiter(func_randid)
            srl.acquire_slot()
            return func(*args, **kwargs)

        return wrapper

    return decorator


def send_msg_ack_to_user(details: SlackMessageInfo, client: WebClient) -> None:
    if details.is_bot_msg and details.sender:
        respond_in_thread(
            client=client,
            channel=details.channel_to_respond,
            thread_ts=details.msg_to_respond,
            receiver_ids=[details.sender],
            text="Hi, we're evaluating your query :face_with_monocle:",
        )
        return

    update_emote_react(
        emoji=DANSWER_REACT_EMOJI,
        channel=details.channel_to_respond,
        message_ts=details.msg_to_respond,
        remove=False,
        client=client,
    )


def handle_message(
    message_info: SlackMessageInfo,
    channel_config: SlackBotConfig | None,
    client: WebClient,
    num_retries: int = DANSWER_BOT_NUM_RETRIES,
    answer_generation_timeout: int = DANSWER_BOT_ANSWER_GENERATION_TIMEOUT,
    should_respond_with_error_msgs: bool = DANSWER_BOT_DISPLAY_ERROR_MSGS,
    disable_docs_only_answer: bool = DANSWER_BOT_DISABLE_DOCS_ONLY_ANSWER,
    disable_auto_detect_filters: bool = DISABLE_DANSWER_BOT_FILTER_DETECT,
    reflexion: bool = ENABLE_DANSWERBOT_REFLEXION,
    disable_cot: bool = DANSWER_BOT_DISABLE_COT,
    thread_context_percent: float = DANSWER_BOT_TARGET_CHUNK_PERCENTAGE,
) -> bool:
    """Potentially respond to the user message depending on filters and if an answer was generated

    Returns True if need to respond with an additional message to the user(s) after this
    function is finished. True indicates an unexpected failure that needs to be communicated
    Query thrown out by filters due to config does not count as a failure that should be notified
    Danswer failing to answer/retrieve docs does count and should be notified
    """
    channel = message_info.channel_to_respond

    logger = cast(
        logging.Logger,
        ChannelIdAdapter(logger_base, extra={SLACK_CHANNEL_ID: channel}),
    )

    messages = message_info.thread_messages
    message_ts_to_respond_to = message_info.msg_to_respond
    sender_id = message_info.sender
    bypass_filters = message_info.bypass_filters
    is_bot_msg = message_info.is_bot_msg
    is_bot_dm = message_info.is_bot_dm

    document_set_names: list[str] | None = None
    persona = channel_config.persona if channel_config else None
    prompt = None
    if persona:
        document_set_names = [
            document_set.name for document_set in persona.document_sets
        ]
        prompt = persona.prompts[0] if persona.prompts else None

    should_respond_even_with_no_docs = persona.num_chunks == 0 if persona else False

    # List of user id to send message to, if None, send to everyone in channel
    send_to: list[str] | None = None
    respond_tag_only = False
    respond_team_member_list = None

    bypass_acl = False
    if (
        channel_config
        and channel_config.persona
        and channel_config.persona.document_sets
    ):
        # For Slack channels, use the full document set, admin will be warned when configuring it
        # with non-public document sets
        bypass_acl = True

    channel_conf = None
    if channel_config and channel_config.channel_config:
        channel_conf = channel_config.channel_config
        if not bypass_filters and "answer_filters" in channel_conf:
            reflexion = "well_answered_postfilter" in channel_conf["answer_filters"]

            if (
                "questionmark_prefilter" in channel_conf["answer_filters"]
                and "?" not in messages[-1].message
            ):
                logger.info(
                    "Skipping message since it does not contain a question mark"
                )
                return False

        logger.info(
            "Found slack bot config for channel. Restricting bot to use document "
            f"sets: {document_set_names}, "
            f"validity checks enabled: {channel_conf.get('answer_filters', 'NA')}"
        )

        respond_tag_only = channel_conf.get("respond_tag_only") or False
        respond_team_member_list = channel_conf.get("respond_team_member_list") or None

    if respond_tag_only and not bypass_filters:
        logger.info(
            "Skipping message since the channel is configured such that "
            "DanswerBot only responds to tags"
        )
        return False

    if respond_team_member_list:
        send_to, _ = fetch_userids_from_emails(respond_team_member_list, client)

    # If configured to respond to team members only, then cannot be used with a /DanswerBot command
    # which would just respond to the sender
    if respond_team_member_list and is_bot_msg:
        if sender_id:
            respond_in_thread(
                client=client,
                channel=channel,
                receiver_ids=[sender_id],
                text="The DanswerBot slash command is not enabled for this channel",
                thread_ts=None,
            )

    try:
        send_msg_ack_to_user(message_info, client)
    except SlackApiError as e:
        logger.error(f"Was not able to react to user message due to: {e}")

    @retry(
        tries=num_retries,
        delay=0.25,
        backoff=2,
        logger=logger,
    )
    @rate_limits(client=client, channel=channel, thread_ts=message_ts_to_respond_to)
    def _get_answer(new_message_request: DirectQARequest) -> OneShotQAResponse:
        action = "slack_message"
        if is_bot_msg:
            action = "slack_slash_message"
        elif bypass_filters:
            action = "slack_tag_message"
        elif is_bot_dm:
            action = "slack_dm_message"

        slack_usage_report(action=action, sender_id=sender_id, client=client)

        max_document_tokens: int | None = None
        max_history_tokens: int | None = None
        if len(new_message_request.messages) > 1:
            # In cases of threads, split the available tokens between docs and thread context
            input_tokens = get_max_input_tokens(GEN_AI_MODEL_VERSION)
            max_history_tokens = int(input_tokens * thread_context_percent)

            remaining_tokens = input_tokens - max_history_tokens

            query_text = new_message_request.messages[0].message
            if persona:
                max_document_tokens = compute_max_document_tokens(
                    persona=persona,
                    actual_user_input=query_text,
                    max_llm_token_override=remaining_tokens,
                )
            else:
                max_document_tokens = (
                    remaining_tokens
                    - 512  # Needs to be more than any of the QA prompts
                    - check_number_of_tokens(query_text)
                )

        with Session(get_sqlalchemy_engine()) as db_session:
            # This also handles creating the query event in postgres
            answer = get_search_answer(
                query_req=new_message_request,
                user=None,
                max_document_tokens=max_document_tokens,
                max_history_tokens=max_history_tokens,
                db_session=db_session,
                answer_generation_timeout=answer_generation_timeout,
                enable_reflexion=reflexion,
                bypass_acl=bypass_acl,
            )
            if not answer.error_msg:
                return answer
            else:
                raise RuntimeError(answer.error_msg)

    try:
        # By leaving time_cutoff and favor_recent as None, and setting enable_auto_detect_filters
        # it allows the slack flow to extract out filters from the user query
        filters = BaseFilters(
            source_type=None,
            document_set=document_set_names,
            time_cutoff=None,
        )

        # Default True because no other ways to apply filters in Slack (no nice UI)
        auto_detect_filters = (
            persona.llm_filter_extraction if persona is not None else True
        )
        if disable_auto_detect_filters:
            auto_detect_filters = False

        retrieval_details = RetrievalDetails(
            run_search=OptionalSearchSetting.ALWAYS,
            real_time=False,
            filters=filters,
            enable_auto_detect_filters=auto_detect_filters,
        )

        # This includes throwing out answer via reflexion
        answer = _get_answer(
            DirectQARequest(
                messages=messages,
                prompt_id=prompt.id if prompt else None,
                persona_id=persona.id if persona is not None else 0,
                retrieval_options=retrieval_details,
                chain_of_thought=not disable_cot,
            )
        )
    except Exception as e:
        logger.exception(
            f"Unable to process message - did not successfully answer "
            f"in {num_retries} attempts"
        )
        # Optionally, respond in thread with the error message, Used primarily
        # for debugging purposes
        if should_respond_with_error_msgs:
            respond_in_thread(
                client=client,
                channel=channel,
                receiver_ids=None,
                text=f"Encountered exception when trying to answer: \n\n```{e}```",
                thread_ts=message_ts_to_respond_to,
            )

        # In case of failures, don't keep the reaction there permanently
        try:
            update_emote_react(
                emoji=DANSWER_REACT_EMOJI,
                channel=message_info.channel_to_respond,
                message_ts=message_info.msg_to_respond,
                remove=True,
                client=client,
            )
        except SlackApiError as e:
            logger.error(f"Failed to remove Reaction due to: {e}")

        return True

    # Got an answer at this point, can remove reaction and give results
    try:
        update_emote_react(
            emoji=DANSWER_REACT_EMOJI,
            channel=message_info.channel_to_respond,
            message_ts=message_info.msg_to_respond,
            remove=True,
            client=client,
        )
    except SlackApiError as e:
        logger.error(f"Failed to remove Reaction due to: {e}")

    if answer.answer_valid is False:
        logger.info(
            "Answer was evaluated to be invalid, throwing it away without responding."
        )
        if answer.answer:
            logger.debug(answer.answer)
        return True

    retrieval_info = answer.docs
    if not retrieval_info:
        # This should not happen, even with no docs retrieved, there is still info returned
        raise RuntimeError("Failed to retrieve docs, cannot answer question.")

    top_docs = retrieval_info.top_documents
    if not top_docs and not should_respond_even_with_no_docs:
        logger.error(
            f"Unable to answer question: '{answer.rephrase}' - no documents found"
        )
        # Optionally, respond in thread with the error message
        # Used primarily for debugging purposes
        if should_respond_with_error_msgs:
            respond_in_thread(
                client=client,
                channel=channel,
                receiver_ids=None,
                text="Found no documents when trying to answer. Did you index any documents?",
                thread_ts=message_ts_to_respond_to,
            )
        return True

    if not answer.answer and disable_docs_only_answer:
        logger.info(
            "Unable to find answer - not responding since the "
            "`DANSWER_BOT_DISABLE_DOCS_ONLY_ANSWER` env variable is set"
        )
        return True

    # If called with the DanswerBot slash command, the question is lost so we have to reshow it
    restate_question_block = get_restate_blocks(messages[-1].message, is_bot_msg)

    answer_blocks = build_qa_response_blocks(
        message_id=answer.chat_message_id,
        answer=answer.answer,
        quotes=answer.quotes.quotes if answer.quotes else None,
        source_filters=retrieval_info.applied_source_filters,
        time_cutoff=retrieval_info.applied_time_cutoff,
        favor_recent=retrieval_info.recency_bias_multiplier > 1,
        skip_quotes=persona is not None,  # currently Personas don't support quotes
    )

    # Get the chunks fed to the LLM only, then fill with other docs
    llm_doc_inds = answer.llm_chunks_indices or []
    llm_docs = [top_docs[i] for i in llm_doc_inds]
    remaining_docs = [
        doc for idx, doc in enumerate(top_docs) if idx not in llm_doc_inds
    ]
    priority_ordered_docs = llm_docs + remaining_docs
    document_blocks = (
        build_documents_blocks(
            documents=priority_ordered_docs,
            message_id=answer.chat_message_id,
        )
        if priority_ordered_docs
        else []
    )

    all_blocks = restate_question_block + answer_blocks + document_blocks

    if channel_conf and channel_conf.get("follow_up_tags") is not None:
        all_blocks.append(build_follow_up_block(message_id=answer.chat_message_id))

    try:
        respond_in_thread(
            client=client,
            channel=channel,
            receiver_ids=send_to,
            text="Hello! Danswer has some results for you!",
            blocks=all_blocks,
            thread_ts=message_ts_to_respond_to,
            # don't unfurl, since otherwise we will have 5+ previews which makes the message very long
            unfurl=False,
        )

        # For DM (ephemeral message), we need to create a thread via a normal message so the user can see
        # the ephemeral message. This also will give the user a notification which ephemeral message does not.
        if respond_team_member_list:
            respond_in_thread(
                client=client,
                channel=channel,
                text=(
                    "👋 Hi, we've just gathered and forwarded the relevant "
                    + "information to the team. They'll get back to you shortly!"
                ),
                thread_ts=message_ts_to_respond_to,
            )

        return False

    except Exception:
        logger.exception(
            f"Unable to process message - could not respond in slack in {num_retries} attempts"
        )
        return True
