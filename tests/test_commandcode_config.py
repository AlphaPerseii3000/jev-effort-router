"""Resolution of the opt-in CommandCode trio — and what "configured" is allowed to mean.

Three settings, one gate. The distinctions matter more than the happy path:

* ``console_jev_commandcode_base_url`` is the whole opt-in — the upstream has no ZDR agreement —
  so an empty URL means *off* and must look exactly like a deployment that never knew the setting,
  even when the other two keys are lying around;
* once the URL names an endpoint, the model and the key variable complete themselves with the
  documented pair (``typesafe/jev``, ``COMMANDCODE_API_KEY``): nothing about them is
  deployment-specific;
* a URL that is not a plain HTTPS endpoint disables the fallback and nothing else — the legacy
  chain is never blocked by a configuration mistake made in the fallback;
* an unusable URL is dropped from the resolved value, so a URL that carried credentials can never
  be echoed by a status line or a log record.
"""

from __future__ import annotations

import logging

import pytest

from config import (
    COMMANDCODE_BASE_URL_SETTING,
    CommandCodeFallback,
    Settings,
    load_settings,
    resolve_commandcode_fallback,
)

CC_URL = "https://api.commandcode.ai/provider/v1/systemone"
CC_MODEL = "typesafe/jev"
CC_KEY_ENV = "COMMANDCODE_API_KEY"


def resolve(**kwargs):
    values = {"base_url": CC_URL, "model": CC_MODEL, "api_key_env": CC_KEY_ENV}
    values.update(kwargs)
    return resolve_commandcode_fallback(**values)


def test_an_empty_trio_is_disabled_and_not_configured():
    fallback = resolve(base_url="", model="", api_key_env="")

    assert fallback == CommandCodeFallback()
    assert fallback.configured is False
    assert fallback.enabled is False
    assert fallback.reason == ""


def test_a_complete_trio_is_enabled():
    fallback = resolve()

    assert fallback.configured is True
    assert fallback.enabled is True
    assert (fallback.url, fallback.model, fallback.key_env) == (CC_URL, CC_MODEL, CC_KEY_ENV)
    assert fallback.reason == ""


def test_surrounding_whitespace_is_trimmed():
    fallback = resolve(base_url=f"  {CC_URL}  ", model=f" {CC_MODEL} ", api_key_env=" COMMANDCODE_API_KEY\n")

    assert (fallback.url, fallback.model, fallback.key_env) == (CC_URL, CC_MODEL, CC_KEY_ENV)


def test_an_empty_url_is_off_even_when_the_other_two_are_set():
    # The URL is the consent to reach a non-ZDR upstream: without it the pair says "what to ask"
    # about an endpoint nobody opted into, which is inert, not an error worth a warning.
    fallback = resolve(base_url="", model=CC_MODEL, api_key_env=CC_KEY_ENV)

    assert fallback == CommandCodeFallback()
    assert fallback.configured is False
    assert fallback.enabled is False
    assert fallback.reason == ""


@pytest.mark.parametrize(
    "kwargs, attribute, expected",
    [
        ({"model": ""}, "model", CC_MODEL),
        ({"api_key_env": ""}, "key_env", CC_KEY_ENV),
    ],
    ids=["model-empty", "key-env-empty"],
)
def test_the_url_alone_completes_the_trio_with_the_documented_defaults(kwargs, attribute, expected):
    fallback = resolve(**kwargs)

    assert fallback.configured is True
    assert fallback.enabled is True
    assert fallback.reason == ""
    assert getattr(fallback, attribute) == expected
    assert fallback.url == CC_URL


@pytest.mark.parametrize(
    "url",
    [
        "http://api.commandcode.ai/provider/v1/systemone",
        "ftp://api.commandcode.ai/provider/v1/systemone",
        "https://user:secret@api.commandcode.ai/provider/v1/systemone",
        "https://api.commandcode.ai/provider/v1/systemone?token=secret",
        "https://api.commandcode.ai/provider/v1/systemone#fragment",
        "https:///provider/v1/systemone",
        "api.commandcode.ai/provider/v1/systemone",
        "not a url at all",
        # ``urlsplit`` itself raises on a bracket it cannot parse: an operator typo must still be
        # an unusable endpoint, not an exception escaping the resolver.
        "https://[broken/provider/v1/systemone",
    ],
)
def test_an_unusable_url_disables_the_fallback_without_keeping_the_value(url):
    fallback = resolve(base_url=url)

    assert fallback.enabled is False
    assert fallback.configured is True
    assert COMMANDCODE_BASE_URL_SETTING in fallback.reason
    # Nothing that could hold a credential survives into the resolved value.
    assert fallback.url == ""
    assert "secret" not in fallback.reason


def test_an_invalid_url_is_dropped_even_when_the_other_two_are_empty():
    # The rejection path must not keep the value: a URL that embedded a credential would
    # otherwise hand it straight to the status line.
    fallback = resolve_commandcode_fallback(
        base_url="https://user:secret@api.commandcode.ai/provider/v1/systemone",
        model="",
        api_key_env="",
    )

    assert fallback.configured is True
    assert fallback.enabled is False
    assert fallback.url == ""
    assert "secret" not in fallback.reason


def test_a_url_with_an_ip_and_port_is_accepted():
    fallback = resolve(base_url="https://127.0.0.1:8443/provider/v1/systemone")

    assert fallback.enabled is True
    assert fallback.url == "https://127.0.0.1:8443/provider/v1/systemone"


# -- how the snapshot exposes it ------------------------------------------------------


def settings_for(config):
    return load_settings(lambda key, default=None: config.get(key, default))


def test_the_three_settings_default_to_empty_and_keep_settings_equality():
    settings = settings_for({})

    assert settings.console_jev_commandcode_base_url == ""
    assert settings.console_jev_commandcode_api_key_env == ""
    assert settings.console_jev_commandcode_model == ""
    assert settings == Settings()


def test_the_loader_reads_the_trio():
    settings = settings_for(
        {
            "console_jev_commandcode_base_url": CC_URL,
            "console_jev_commandcode_api_key_env": CC_KEY_ENV,
            "console_jev_commandcode_model": CC_MODEL,
        }
    )

    fallback = settings.commandcode_fallback()
    assert fallback.enabled is True
    assert fallback.url == CC_URL


def test_only_the_console_decider_can_use_the_trio():
    config = {
        "console_jev_commandcode_base_url": CC_URL,
        "console_jev_commandcode_api_key_env": CC_KEY_ENV,
        "console_jev_commandcode_model": CC_MODEL,
    }

    assert settings_for({**config, "decider": "jev_console"}).active_commandcode_fallback().enabled is True
    for decider in ("jev", "openai_compatible"):
        settings = settings_for({**config, "decider": decider})
        # Configured, yes — but invisible to a decider that does not own it.
        assert settings.commandcode_fallback().enabled is True
        assert settings.active_commandcode_fallback() == CommandCodeFallback()


def test_opting_in_with_the_url_alone_warns_about_nothing(caplog):
    # The defaults are the documented pair, so a profile that names only the endpoint is complete:
    # no warning, and the resolved value names the key variable it will read.
    with caplog.at_level(logging.WARNING, logger="config"):
        settings = settings_for({"console_jev_commandcode_base_url": CC_URL})

    assert caplog.records == []
    fallback = settings.commandcode_fallback()
    assert fallback.enabled is True
    assert (fallback.model, fallback.key_env) == (CC_MODEL, CC_KEY_ENV)


# -- a URL that the parser itself rejects ---------------------------------------------

# ``urlsplit`` raises ValueError on an unclosed IPv6 bracket before any of the explicit checks can
# look at the value, so this is the case that used to escape the resolver and break the loader.
BROKEN_URL = "https://[broken/provider/v1/systemone"


def test_the_loader_survives_a_url_that_urlsplit_cannot_parse(caplog):
    with caplog.at_level(logging.WARNING, logger="config"):
        settings = settings_for(
            {
                "console_jev_commandcode_base_url": BROKEN_URL,
                "console_jev_commandcode_api_key_env": CC_KEY_ENV,
                "console_jev_commandcode_model": CC_MODEL,
            }
        )

    fallback = settings.commandcode_fallback()
    assert fallback.configured is True
    assert fallback.enabled is False
    assert fallback.url == ""
    assert COMMANDCODE_BASE_URL_SETTING in fallback.reason
    assert COMMANDCODE_BASE_URL_SETTING in caplog.text
    # The raw value is echoed nowhere: not in the reason, not in the warning.
    assert "broken" not in fallback.reason
    assert "broken" not in caplog.text
    # Only the fallback is affected; the rest of the snapshot is the legacy defaults.
    assert settings.decider == "jev"
    assert settings.active_commandcode_fallback() == CommandCodeFallback()


def test_a_trio_with_an_unparsable_url_and_no_model_also_loads():
    settings = settings_for(
        {
            "console_jev_commandcode_base_url": BROKEN_URL,
            "console_jev_commandcode_model": CC_MODEL,
            "console_jev_commandcode_api_key_env": "",
        }
    )

    fallback = settings.commandcode_fallback()
    assert fallback.configured is True
    assert fallback.enabled is False
    assert fallback.url == ""
    assert COMMANDCODE_BASE_URL_SETTING in fallback.reason
