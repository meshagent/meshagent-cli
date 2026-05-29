# ruff: noqa: E402

from __future__ import annotations

import asyncio
import os
import random
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal, Sequence

from rich.text import Text


def _suppress_textual_debug_features() -> None:
    raw_features = os.environ.get("TEXTUAL")
    if raw_features is None:
        return

    parsed_features = [
        value.strip() for value in raw_features.split(",") if value.strip() != ""
    ]
    if len(parsed_features) == 0:
        return

    filtered_features = [
        value for value in parsed_features if value.lower() not in ("debug", "devtools")
    ]
    if len(filtered_features) == len(parsed_features):
        return

    if len(filtered_features) == 0:
        os.environ.pop("TEXTUAL", None)
        return

    os.environ["TEXTUAL"] = ",".join(filtered_features)


_suppress_textual_debug_features()

from textual.app import App, ComposeResult
from textual._context import active_app
from textual import events
from textual.binding import Binding
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from meshagent.cli.tool_integrations import CodexProfileConflictError

from .setup_splash_frames import (
    SETUP_SPLASH_VARIANTS,
    SetupSplashVariant,
    load_setup_splash_frames,
)

LOGIN_LAUNCH_OPTION_ID = "__login_launch__"
LOGIN_EXIT_OPTION_ID = "__login_exit__"
ACCOUNT_CONTINUE_OPTION_ID = "__account_continue__"
ACCOUNT_SWITCH_OPTION_ID = "__account_switch__"
ACCOUNT_EXIT_OPTION_ID = "__account_exit__"
CODEX_CONTINUE_OPTION_ID = "__codex_continue__"
CODEX_UPDATE_OPTION_ID = "__codex_update__"
CODEX_REMOVE_OPTION_ID = "__codex_remove__"
CODEX_CREATE_OPTION_ID = "__codex_create__"
CODEX_SKIP_OPTION_ID = "__codex_skip__"
CODEX_CONFLICT_UPDATE_OPTION_ID = "__codex_conflict_update__"
CODEX_CONFLICT_REMOVE_OPTION_ID = "__codex_conflict_remove__"
CODEX_CONFLICT_CANCEL_OPTION_ID = "__codex_conflict_cancel__"
CODEX_DEFAULT_NONE_OPTION_ID = "__codex_default_none__"
CODEX_DEFAULT_PROFILE_OPTION_ID_PREFIX = "__codex_default_profile__:"
CLAUDE_CONFIGURE_OPTION_ID = "__claude_configure__"
CLAUDE_REMOVE_OPTION_ID = "__claude_remove__"
CLAUDE_SKIP_OPTION_ID = "__claude_skip__"
SAMPLE_CREATE_OPTION_ID = "__sample_create__"
SAMPLE_SKIP_OPTION_ID = "__sample_skip__"
PROJECT_CREATE_OPTION_ID = "__project_create__"
PROJECT_EXIT_OPTION_ID = "__project_exit__"
API_KEY_CREATE_OPTION_ID = "__api_key_create__"
API_KEY_SKIP_OPTION_ID = "__api_key_skip__"
ERROR_EXIT_OPTION_ID = "__error_exit__"


def _codex_default_profile_option_id(profile_id: str) -> str:
    return f"{CODEX_DEFAULT_PROFILE_OPTION_ID_PREFIX}{profile_id}"


def _codex_default_profile_id_from_option_id(option_id: str) -> str | None:
    if not option_id.startswith(CODEX_DEFAULT_PROFILE_OPTION_ID_PREFIX):
        return None
    return option_id.removeprefix(CODEX_DEFAULT_PROFILE_OPTION_ID_PREFIX)


def _tool_proxy_setup_message(*, tool_name: str) -> str:
    return (
        f"{tool_name} was detected on this machine. Configure {tool_name} to use "
        "the MeshAgent proxy so you can centralize OpenAI and Anthropic billing, "
        "usage analytics, and governance in your MeshAgent account instead of "
        "managing separate provider subscriptions."
    )


def _tool_proxy_access_required_message(*, tool_name: str) -> str:
    return (
        f"{tool_name} was detected on this machine. The MeshAgent proxy lets your "
        "team centralize OpenAI and Anthropic billing, usage analytics, and "
        "governance in MeshAgent instead of managing separate provider "
        "subscriptions. Your MeshAgent account is not currently configured for "
        "LLM access for this project. Talk to your account administrator to turn "
        "it on, then run setup again."
    )


def _tool_proxy_affirmative_option_label(*, tool_name: str) -> str:
    return f"Yes, make MeshAgent the default for {tool_name}"


def _tool_proxy_skip_option_label(*, tool_name: str, launch_command: str) -> str:
    return (
        f'No, I will use "meshagent launch {launch_command}" if I want to use '
        f"{tool_name} via MeshAgent."
    )


MESHAGENT_SETUP_LOGO_LINES: tuple[str, ...] = (
    "                                                                                ",
    "                                                                                ",
    "                                                                                ",
    "                                                                                ",
    "                                                                                ",
    '                  -"l1LCy2555222333333yyyyyyfffwwwwCCJ#Tuo!c|_                  ',
    "               `cfG@NR0QQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQN8g1=               ",
    "              cV@HHBWMRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRR0QQQHw,             ",
    "             eA&k&DNMRRMRMMMMMRRRRMMMRRRRRRRRRRRRRRRRRRRRRRRRRRR0QO|            ",
    "            rZPXYKNRMMMMMMMMMMMRR00000000000QQQ00QQQQ00RRRRRRRRRRRQE`           ",
    "            3Pgg&DRMMMMMMMMMMR0MKkXVhhVVhhhgggggggVgb&H0Q0RRRRRRRRRM\\           ",
    "           `qGPPUWRMMMMMMMMRR$61lxcrsrrrrrrllcccxx%%%l{emDQRRRRRRRR0x           ",
    '           `SbGG8WRMMMMMMMMB6r^>vi))))>>>>>>><<<\\))|"++==%VQRRRRRRRRx           ',
    '           .qbGZ&WRMMMMMNNDyi;%<||)))))|||||||""<^\',==^;;,;40RRMMMMR%           ',
    "           .qbZZ&WMNMMNNWDbr/<\\=+++/+=+++++++=)ppj: ^=,,,,`[QMMMMMRR%           ",
    "           .qYZZ&BMNNNNWD@4);)+;;;^;|v+-:^;;;'?@Y5+ /^''''`}0MMMMMMR%           ",
    "           .SkZZ&BMNNNWBHU4);)^:,,')4VT, /,::-}WU2^ |;____.I0MMMMMMR%           ",
    "           .qkbb&BMNNNWBKU4),);_''`*HAS)_),___^n#%^+\"_____ I0MMMMMMRv           ",
    "            qkbbABMNNWWDK8gv,>,___.{D$4<'|'___-`'=/,------ I0MMMMMMMv           ",
    "            qOYY&BNNNWWDK8E),>,---.sB@4>:|'--_`;\\^`__----- ?0MMMMMMMi           ",
    "            qOYYADNNWWBDKUE),<:-_-.{WKE),\"'--.vd4w+ ,_---- !0MMMMMMMi           ",
    "            qAkkADMNNWWDHUd\\,<:---.sW@4<,|'-- aD&Ei =,`--- !0MMMMMMM)           ",
    "            qAkkADNNNWWDH$Pc`^'``` !NKGr:,`.  JW$Oo'--...  w0MMMMMMN)           ",
    '            qAOOADMNNWWBH$OS]<"++)[AWK&V7lvv*yHBK8b3!l%%{ehNMMMMMMMN)           ',
    "            6&OOODMNNNWWDH$AkZGbAKRNBDKU8&UHN0NWDK$U&AA$BR0MMMMMMMRW)           ",
    "     .  ... 7&YYkKMNNNNWWBDDHDBWNNNWWWWWWWWNNNNWWWBBDBBWWNNMMMMMMM0b-           ",
    "   . .......;6bGb8NNNNNNNWWWWWWNNNNNNNNNNNNNNMNNNNNWNNNNNNMMMMNMM0Dl ....       ",
    "  .......```.^TEgZ$WMNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNMMNNNNMNMMR00bx .........   ",
    " ......````-_--vupEAHNMMNNNNNNNNNNNNNNNNNMMMMMMMMMRRRRRRRR00QRHEt;.--````...... ",
    "  .....```````` .;v1y4ADMR0000000000RRRMMMNWWBDHKK@$U&&OkbXqJ!<'-':'___-```.... ",
    " ....````--------`. `:\"%]Lw5F52ywJTnLzoet1]!I}*srllx%%v)>\"+;;^^^;,:''__--``....",
    "....``--__'':,,;^^=++////\"|))\\\\<\\<>>>>>>)))))))>>><\\))|\"/++=^;,,:''__--``....",
    " ....```--__''::,;^^=++//\"\"|||))))))\\\\)))))))))|||\"\"//+++=^;;,,,:''___--``.....",
    " ......```--___'':::,,;;^^^^^======++++++++++=+++===^^;;;,,,,:'''____-````......",
    "    ......````---____'''''::,,,,,,,,,,,,,,,,,,,,,,::::'''''_____---````.......  ",
    "      ........```````-`--________''''''''''''_________------```````.........    ",
)

MESHAGENT_SETUP_LOGO_COLOR_HEX_LINES: tuple[str, ...] = (
    "#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737",
    "#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737",
    "#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737",
    "#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737",
    "#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737",
    "#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#393939#434243#505050#5e5d5e#69696a#727172#767576#777777#777777#777777#767676#767676#757575#757475#747474#747474#737272#727272#727272#717171#717171#717171#707070#707070#706f6f#6f6f6f#6f6f6f#6e6e6e#6e6d6d#6d6c6c#6c6c6c#6c6c6c#6a6a6a#696969#686868#666566#636363#5f5f5f#575757#4b4b4b#414141#3a3a3a#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737",
    "#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#414141#6f6f6f#a9a9aa#d0d0d3#e9e7ea#f4f3f6#f9f9fb#ffffff#ffffff#ffffff#ffffff#ffffff#ffffff#ffffff#ffffff#ffffff#ffffff#ffffff#ffffff#ffffff#ffffff#ffffff#ffffff#ffffff#ffffff#ffffff#ffffff#ffffff#ffffff#ffffff#ffffff#ffffff#ffffff#ffffff#ffffff#ffffff#ffffff#ffffff#ffffff#ffffff#ffffff#ffffff#ffffff#fdfbfd#eae9ea#c6c4c6#8a8a8a#505050#383838#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737",
    "#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#414141#919193#d1d1d6#d9d9dc#d9d9de#e1dfe2#e7e7e9#efeff1#f1f1f3#f1f1f3#f1f1f3#f1eff3#f1eff3#f1eff3#f1eff3#f1f1f3#f1f1f3#f1f1f3#f1f1f4#f1f1f3#f1eff3#f1eff3#f1f1f3#f1f1f3#f1f1f3#f1f1f3#f1f1f3#f1f1f3#f3f1f4#f1f1f4#f1f1f3#f3f3f4#f3f3f4#f3f3f4#f3f3f4#f4f3f4#f4f3f6#f4f3f6#f4f4f6#f4f4f6#f4f4f6#f4f4f6#f4f4f6#f3f3f6#f3f3f4#f4f4f6#f6f6f8#fbf9fd#ffffff#ffffff#d9d9d9#6b6b6b#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737",
    "#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#535354#bcbcc0#c0c0c4#b5b5b9#c3c1c6#dcdcdf#eae9ec#efeff1#f1eff3#f1eff3#efeff3#f1eff3#efeff1#efeef1#efeff1#efeff3#efeff3#f1eff3#f1eff3#f1f1f3#f1eff3#efeff3#efeff3#efeff3#f1eff3#f1eff3#f1eff3#f1eff3#f1f1f3#f3f1f4#f3f1f4#f3f1f4#f3f1f4#f3f1f4#f3f1f4#f3f1f4#f1f1f3#f3f1f4#f3f3f4#f4f3f4#f4f4f6#f4f4f6#f4f3f6#f4f4f6#f4f4f6#f4f4f6#f4f4f6#f4f4f6#f4f3f4#f3f3f4#f3f3f4#f8f8f9#ffffff#b8b8b8#3a3a3a#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737",
    "#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#434343#aaaaae#a3a3a7#a2a2a4#b1b1b4#d6d6d9#ececee#f1f1f3#efeff1#efeff1#efeff3#efeff3#f1eff1#efeff1#efeff3#efeff3#efeff1#efeef1#efeff1#f1eff3#f3f3f6#f6f6f8#f9f8fb#f9f8fb#f8f8f9#f6f6f9#f6f6f9#f6f6f9#f8f8fb#f9f9fb#f9f8fb#f9f9fb#fbf9fd#fbf9fd#fbf9fd#f9f9fd#f9f9fd#fbfbfd#fbfbff#fbfbff#fbfbfd#f9f9fb#f6f6f8#f4f3f6#f3f3f4#f4f3f4#f4f3f6#f4f3f6#f4f3f6#f3f3f4#f3f3f4#f3f1f4#f1f1f4#f1f1f3#ffffff#9c9c9d#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737",
    "#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#737375#a5a5a9#9e9da0#9d9d9f#c1c1c4#dfdfe2#f3f3f4#efeff1#efeff3#efeff3#efeff1#efeef1#efeef1#efeef1#efeff1#efeff1#efeff1#f3f3f4#f8f8f9#efeff3#d4d4d7#b6b6b9#9f9ea0#929293#908f90#8f8f90#909091#909091#8f8f90#8f8f90#8e8e8f#8d8c8d#8b8b8c#8b8a8b#8c8b8c#8b8a8b#8b8b8b#8b8b8b#919192#9e9e9f#adadae#c1c1c3#dadadc#f6f6f8#ffffff#f8f6f9#f3f3f4#f3f3f4#f3f3f4#f3f3f4#f3f1f4#f1f1f3#f1f1f3#f1f1f3#f3f3f4#eeeeef#3b3b3b#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737",
    "#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#848486#a9a9ac#a3a3a5#a4a4a7#c7c6ca#e7e7e9#f1eff3#efeff1#efeef1#efeef1#efeff1#efeef1#eeeef1#eeeef1#efeef1#f1eff3#f4f4f6#cbcbcd#808081#4f4f4f#424242#404040#414141#434343#444444#434343#434343#434343#434343#434343#434343#424242#424141#414141#414141#414141#404040#404040#3f3f3f#3f3f3f#3f3f3f#424141#464545#555555#838283#dcdcde#ffffff#f3f1f4#f3f1f4#f1f1f3#f1f1f3#f1f1f3#f1f1f3#f1eff3#f1eff3#f6f4f8#404040#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737",
    "#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#878688#aeadb1#a8a8aa#a8a8aa#c4c4c7#e6e6e7#f1eff3#efeff1#efeff1#eeeef1#eeeef1#eeeeef#eeecef#eeeeef#efeef1#e1e1e4#808082#434343#383838#3c3c3c#3e3e3e#3d3d3d#3d3d3d#3c3d3c#3d3d3d#3d3d3c#3c3c3c#3c3c3c#3c3c3c#3c3c3c#3c3c3c#3c3c3c#3c3c3c#3c3c3c#3c3c3c#3b3b3b#3b3b3b#3b3a3a#3a3a3a#3a3a3a#393939#393939#383838#383838#383838#3f3f3f#929293#fbfbfd#f1f1f3#f1f1f3#f1eff3#f1f1f3#f1f1f3#f1eff3#f1eff3#f4f3f6#404040#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737",
    "#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#868688#b0b0b2#a9a9ad#aaaaad#c1c0c4#e4e4e6#f1eff3#efeef1#eeeef1#eeeef1#eeeeef#eeecef#eceaee#eae9ec#dfdfe2#717173#3d3d3d#383838#3f3f3f#3b3b3b#3a3a3a#3a3a3a#3a3a3a#3a3a3a#3a3a3a#3a3a3a#3a3a3a#3a3a3a#3a3a3a#3a3a3a#3a3a3a#3a3a3a#3a3a3a#3a3a3a#3a3939#393939#3b3b3c#383838#373737#373737#383838#383838#383838#383838#383838#373737#383737#989798#fbf9fb#f1eff3#f1eff3#efeff1#efeef1#efeef1#efeff1#f3f3f4#3f3f3f#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737",
    "#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#868688#b0b0b4#aaa9ac#acaaae#c1c1c4#e4e4e6#efeef1#ececef#eeecef#eeecef#eceaee#eae9ec#e6e6e7#dfdee1#b0aeb1#434343#393939#3c3c3c#3b3b3b#383838#383838#393938#393939#393939#383838#383838#383838#393938#393939#383838#383838#383838#383838#383838#3d3d3d#7d7d80#7d7d7f#5b5b5c#373737#373737#383838#383838#373737#373737#373737#373737#373737#4d4d4e#fdfdff#efeff1#efeff1#efeff1#efeef1#efeef1#f1f1f3#f3f3f4#3f3f3f#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737",
    "#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#868587#b2b1b5#aaa9ad#aaa9ad#c1c1c4#e2e2e4#eeecef#eceaee#eaeaee#eaeaec#e9e9ec#e6e6e9#dfdfe2#d0d0d3#979698#3d3d3d#383838#3d3d3d#393939#383838#383838#383838#383838#383838#3a3a3a#3e3e3e#383838#373737#373737#383838#383838#383838#383838#373737#494949#d0d0d3#b1b1b4#787879#393939#373737#393939#383838#373737#373737#373737#373737#373737#484848#f8f8fb#efeff1#efeff1#efeff1#efeef1#eeeeef#efeff1#f1f1f4#3f3f3f#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737",
    "#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#88878a#b5b5b8#acacb0#acacae#c0c0c4#e2e2e4#eeeeef#eceaee#eae9ec#e9e7ea#e7e7ea#e2e2e6#d9d9dc#c7c7ca#979799#3d3d3d#383838#3d3d3d#383838#373737#373737#373737#373737#3d3d3d#969698#919092#636364#373737#373737#393939#373737#373737#373737#373737#474747#e4e4e7#cacacd#757577#383838#373737#3a3a3a#383838#373737#373737#373737#373737#373737#484848#f8f8fb#efeff1#efeff1#efeff1#efeef1#eeeef1#efeff1#f1f1f4#3f3f3f#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737",
    "#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#868688#b6b6b9#aeadb1#aeadb1#c0c0c3#e2e2e4#eeeeef#eceaee#eae9ec#e9e9ea#e6e6e9#e1dfe2#d6d4d9#c7c6c8#989799#3d3d3d#373737#3d3d3c#383838#373737#373737#373737#373737#464646#dad9dc#bdbcbf#88888b#3a3a3a#373737#3a3a3a#373737#373737#373737#373737#383838#606061#656566#3f3f3f#383838#393939#3a3939#373737#373737#373737#373737#373737#373737#494849#f9f8fb#efeff1#efeff1#efeef1#efeef1#eeeeef#efeff1#f1eff3#3e3e3e#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737",
    "#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#848385#b6b6b9#b0aeb2#aeaeb1#bfbfc1#e1e1e4#eeecef#eaeaec#eae9ec#e7e7ea#e4e4e6#dfdee1#d4d3d6#c6c4c7#9d9c9e#3e3e3e#373737#3c3c3c#373737#373737#373737#373737#373737#444444#dfdfe2#cbcbce#969798#3c3c3c#373737#3a3a3a#373737#373737#373737#373737#373737#373737#373737#383838#393939#373737#373737#373737#373737#373737#373737#373737#373737#494949#f8f8fb#eeeef1#efeff1#efeef1#eeeeef#efeef1#efeff1#efeef1#3e3e3e#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737",
    "#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#858486#b9b9bc#b1b0b4#b1b0b4#c0c0c3#e2e1e2#ececee#e9e9ec#e9e7ea#e6e6e9#e4e2e6#dfdee1#d4d4d7#c6c6c8#99999c#3d3d3d#373737#3c3c3c#373737#373737#373737#373737#373737#444444#e2e2e6#d1d1d4#98989a#3c3c3c#373737#3a3a3a#373737#373737#373737#373737#373737#383838#3b3b3b#383838#373737#373737#373737#373737#373737#373737#373737#373737#373737#4a4a4a#f9f9fd#efeef1#efeff1#efeef1#eeeeef#eeeeef#efeff1#efeff1#3e3e3e#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737",
    "#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#848385#bababd#b2b2b5#b4b2b6#bdbdc0#dfdfe1#ececee#eae9ec#e7e7ea#e6e6e9#e2e2e6#dedcdf#d6d4d9#c7c7ca#99989c#3d3d3d#373737#3c3c3c#373737#373737#373737#373737#373737#444444#e4e4e7#d3d1d4#9a9a9d#3c3d3d#373737#393939#373737#373737#373737#373737#3e3e3e#939295#969597#6d6d6f#383838#373737#373737#373737#373737#373737#373737#373737#373737#4b4b4b#f9f9fb#eeeeef#efeff1#efeef1#eeeeef#eeecef#efeff1#eeecef#3d3d3d#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737",
    "#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#848485#bdbcc0#b5b5b8#b5b5b8#bcbcc0#dfdee1#eeecef#eaeaec#e9e9ea#e7e6e9#e4e2e6#dfdee1#d7d7da#c8c7ca#959397#3b3b3b#373737#3b3b3b#373737#373737#373737#373737#373737#444444#e6e4e7#d1d1d4#98989a#3b3c3c#373737#3a3a3a#373737#373737#373737#373737#535353#dcdcdf#c0c0c3#9a9a9d#3d3d3e#373737#383838#373737#373737#373737#373737#373737#373737#4b4b4b#f9f9fd#eeeeef#efeeef#efeeef#eeeeef#eeeeef#efeff1#eeecef#3d3d3d#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737",
    "#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#848385#bfbdc1#b6b6b9#b6b6b9#bcbcbf#dedcdf#ececee#eaeaee#e9e7ea#e7e6e9#e4e4e6#dfdfe2#d7d7da#cbcacd#a3a3a5#414141#373737#383838#373737#373737#373737#373737#373737#4b4b4b#e9e9ec#d3d3d6#a9a9ac#434344#373737#373737#373737#373737#373737#373737#666667#e7e6e9#ceced1#b8b8ba#565657#373737#373737#373737#373737#373737#373737#373737#373737#6c6b6c#f8f8fb#eeecef#efeef1#efeeef#eeeeef#eeecef#efeef1#ececee#3d3d3d#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737",
    "#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#858586#bfbfc3#b9b8bc#b8b8ba#bcbcbf#dedcdf#eeecef#eaeaec#e9e9ea#e7e6ea#e6e6e7#e2e1e4#dad9de#cdcdd0#bab9bc#87878a#4d4d4d#3c3b3c#393939#383838#383838#3a3a3a#4f4e4f#bcbcbf#e4e4e7#d4d3d6#c0c0c3#929293#585758#414141#3e3e3e#3f3e3f#464646#707071#d9d9dc#e2e1e4#d6d4d7#c6c4c7#adadb0#727173#4c4c4c#424242#3f3f3f#3f3f3f#444444#545455#8e8d8e#ececef#efeef1#eeeeef#eeeeef#eeeeef#eeeeef#eeecef#efeef1#ececef#3d3d3d#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737",
    "#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#7f7e80#c0c0c3#b9b8bc#b8b8ba#bab9bd#dededf#eeeeef#eaeaee#eae9ec#e9e7ea#e7e6e9#e4e4e6#dfdfe2#d9d7dc#cdcbd0#bfbdc0#b6b6b8#acacae#a7a7a9#adacae#bcbabd#d6d4d7#f1f1f4#eceaee#e2e1e4#dcdcdf#d4d3d6#cac8cb#c4c4c7#c3c1c4#c8c8cb#d9d9dc#ececef#f6f4f8#eae9ea#e4e2e6#dcdcdf#d4d3d6#cbcbce#c7c7ca#c1c0c3#bcbcbf#bfbfc1#cdcdd0#e2e1e4#f3f3f6#f8f6f9#efeef1#eeeeef#eeeeef#eeeeef#eeeeef#eeeeef#eeeeef#f1eff3#e7e6e9#3b3a3a#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737",
    "#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#585859#c0c0c3#b2b2b5#b4b2b6#b5b4b8#d6d6d7#efeef1#ececee#eceaee#eae9ec#e9e7ea#e7e6e9#e6e4e7#e2e2e6#dfdfe2#dedcdf#dadade#dcdcdf#e1e1e4#e4e4e7#e9e9ec#eceaee#e9e9ec#e7e7ea#e6e6e9#e4e4e7#e4e2e6#e4e2e6#e4e4e6#e6e6e7#e7e7e9#eae9ec#eceaee#eaeaec#eae9ec#e7e7e9#e6e6e7#e4e2e6#e1dfe2#e1dfe2#dfdfe2#e1e1e2#e2e2e6#e6e4e7#e7e7ea#eae9ec#ececee#eeecef#eeeeef#eeecef#eeecef#eeeeef#eeeeef#eeecef#f9f9fd#aeadb0#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737",
    "#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#7f7e80#aeaeb1#a7a7a9#adadb0#c4c4c6#e9e9ea#ececef#eaeaee#eaeaee#eaeaec#e9e9ea#e9e9ea#e7e7ea#e6e6e9#e6e6e9#e6e6e7#e6e6e7#e7e7ea#e9e9ec#eaeaee#eaeaee#eaeaec#eaeaec#eaeaec#eae9ec#eae9ec#eae9ec#e9e9ec#eae9ec#eaeaec#eceaee#ececee#eeecef#eceaee#eae9ec#e9e9ea#e9e9ea#e9e7ea#e7e7ea#e9e9ea#e9e9ec#eae9ec#eaeaec#eaeaec#eceaee#eeecef#eeeeef#eeeeef#eeecef#ececee#eeecef#eeeeef#f8f8fb#dededf#424242#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737",
    "#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#383838#646465#99999d#9e9ea0#acacae#cbcbcd#e6e6e7#eeeeef#ececee#eaeaee#eaeaec#eaeaec#eceaee#eaeaee#eaeaec#eaeaec#eaeaee#eaeaee#eaeaee#ececee#ececee#eceaee#eceaee#eceaee#eceaee#eaeaec#eaeaec#eaeaee#ececee#ececef#ececee#ececee#ececee#ececee#eceaee#eceaee#eceaee#eaeaee#eceaee#eeecef#eeeeef#ececee#ececee#ececee#ececee#eeecef#ececef#eeecef#eeeeef#f1eff3#f8f6f9#f8f6f9#aeaeb1#404041#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737",
    "#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#3e3e3e#5e5f60#7d7d80#9c9a9e#bfbfc1#dadade#eceaee#efeff3#eeeeef#ececee#eceaee#eaeaee#eaeaee#eaeaee#eaeaee#eaeaee#eaeaec#eceaee#ececee#ececef#ececee#ececef#ececef#ececee#ececee#ececef#eeecef#eeeeef#eeeeef#eeeef1#eeeef1#efeef1#efeff1#efeff3#efeff3#f1f1f3#f1f1f3#f3f1f4#f3f1f4#f3f3f4#f3f3f4#f3f3f6#f4f4f8#f8f6f9#f9f8fb#fbf9fd#f4f4f6#d9d9dc#9a9a9d#525252#383838#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737",
    "#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#383838#3e3e3f#505051#717072#969697#bdbcbf#dcdcde#eeecef#f3f3f6#f6f4f8#f6f6f9#f6f6f8#f6f6f9#f8f6f9#f8f6f9#f6f6f9#f6f6f9#f6f6f8#f6f4f8#f4f4f6#f3f3f6#f3f1f4#efeff1#efeef1#eeecef#eaeaec#e7e7e9#e6e4e7#e2e1e4#dedcdf#d9d9da#d6d6d7#d3d1d4#d0d0d1#cbcbcd#c7c6c8#c3c3c6#c0bfc1#bababd#b5b4b6#adadae#9f9ea0#868687#686869#4c4c4c#3b3b3b#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737",
    "#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#393939#3f3f40#4d4d4d#5d5d5e#6b6b6c#777678#7b7b7c#78787a#757577#717172#6d6d6e#686869#646465#616162#5d5d5d#59595a#575757#545455#525252#4f4f4f#4d4d4d#4b4b4b#484949#474748#464546#444444#434344#424243#414142#404041#3f3f40#3f3f3f#3e3e3e#3d3d3d#3c3c3c#3b3b3b#39393a#383838#383838#383838#383838#383838#383838#383838#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737",
    "#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#383838#383838#383838#383838#383838#393939#393939#393939#393939#393939#39393a#3a3a3a#3a3a3a#3a3a3b#3b3b3b#3b3b3b#3b3b3c#3b3b3c#3b3b3c#3b3b3c#3c3c3c#3c3c3c#3c3c3d#3c3c3d#3c3c3c#3c3c3d#3c3c3d#3d3d3d#3d3d3d#3d3d3d#3d3d3d#3d3d3d#3d3d3d#3d3d3d#3c3c3d#3c3c3c#3c3c3c#3b3b3c#3b3b3b#3b3b3b#3a3a3b#3a3a3a#3a3a3a#393939#393939#393939#383839#383838#383838#383738#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737",
    "#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#383838#383838#383838#383838#383838#393939#393939#393939#39393a#39393a#3a3a3a#3a3a3a#3a3a3a#3a3a3a#3a3a3a#3a3a3b#3a3a3b#3a3a3b#3a3a3b#3b3b3b#3b3a3b#3b3b3b#3a3a3b#3a3a3a#3a3a3b#3a3a3b#3a3a3b#3a3a3b#3a3a3a#3a3a3a#3a3a3a#3a3a3a#3a3a3a#3a3a3a#39393a#393939#393939#393939#393939#383839#383838#383838#383838#383838#383838#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737",
    "#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373738#383838#383838#383838#383838#383838#383838#383838#383838#383838#383838#383838#383838#383838#383839#383839#393839#383839#383838#383838#383838#383838#383838#383838#383838#383838#383838#383838#383838#383838#383838#383838#383838#383838#373738#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737",
    "#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737",
    "#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737#373737",
)


@dataclass(frozen=True, slots=True)
class SetupProject:
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class SetupWizardResult:
    status: Literal["completed", "canceled", "error"]
    message: str | None = None
    project_id: str | None = None
    create_sample: bool = False


@dataclass(frozen=True, slots=True)
class SetupClaudeConfiguration:
    configured: bool
    project_id: str | None = None


LoginStatusHandler = Callable[[str], Awaitable[None] | None]
LoginOperation = Callable[[LoginStatusHandler], Awaitable[None]]
ListProjectsOperation = Callable[[], Awaitable[Sequence[SetupProject]]]
CreateProjectOperation = Callable[[str], Awaitable[str]]
ActivateProjectOperation = Callable[[str], Awaitable[str]]
HasActiveApiKeyOperation = Callable[[str], Awaitable[bool]]
CreateApiKeyOperation = Callable[[str, str], Awaitable[None]]
HasLlmProxyAccessOperation = Callable[[str], Awaitable[bool]]
ListExistingCodexProfilesOperation = Callable[[str], Awaitable[Sequence[str]]]
ConfigureCodexProfileOperation = Callable[[str], Awaitable[None]]
ReplaceCodexProfileOperation = Callable[[str], Awaitable[None]]
RemoveCodexProfileOperation = Callable[[str], Awaitable[None]]
GetCurrentCodexDefaultProfileOperation = Callable[[str], Awaitable[str | None]]
ConfigureCodexDefaultProfileOperation = Callable[[str, str | None], Awaitable[None]]
ConfigureClaudeOperation = Callable[[str], Awaitable[None]]
InspectClaudeConfigurationOperation = Callable[[], Awaitable[SetupClaudeConfiguration]]
ClearClaudeOperation = Callable[[], Awaitable[None]]

SetupMode = Literal[
    "intro",
    "account_choice",
    "login_choice",
    "login_progress",
    "projects",
    "project_name",
    "api_key_choice",
    "api_key_name",
    "codex_choice",
    "codex_profile_name",
    "codex_profile_conflict",
    "codex_default_choice",
    "claude_choice",
    "sample_choice",
    "busy",
    "error",
    "done",
]


class SetupWizardApp(App[None]):
    _SHIMMER_BAND_RADIUS = 8
    _SHIMMER_STEP = 3
    _SHIMMER_INTERVAL_SECONDS = 0.016
    _DISSOLVE_STEPS = 28
    _DISSOLVE_INTERVAL_SECONDS = 0.022
    _FADE_STEPS = 44
    _FADE_INTERVAL_SECONDS = 0.02
    _AUTH_URL_PREFIX = "Waiting for auth redirect on "
    _LOGO_HORIZONTAL_PADDING_COLUMNS = 4
    _LOGO_RESERVED_LOGIN_ROWS = 14
    _LOGO_MIN_DECENT_ROWS = 18
    _LOGO_MIN_DECENT_COLUMNS = 48

    CSS = """
    Screen {
        layout: vertical;
        align: left top;
        padding: 1 2;
        background: #050911;
    }
    #setup-logo {
        width: 100%;
        content-align: center middle;
        margin: 0 0 1 0;
    }
    #setup-title {
        width: 100%;
        content-align: left middle;
        color: #f4f7ff;
        text-style: bold;
        margin: 0 0 1 0;
    }
    #setup-message {
        width: 100%;
        content-align: left middle;
        color: #cad6f4;
        margin: 0 0 1 0;
    }
    #setup-options {
        width: 100%;
        height: 1fr;
        border: round #7ca9ff;
        background: #0a1120;
    }
    #setup-input {
        width: 100%;
        border: round #7ca9ff;
        background: #0a1120;
        margin: 0 0 1 0;
    }
    #setup-status {
        width: 100%;
        min-height: 5;
        color: #cad6f4;
    }
    #setup-url {
        width: 100%;
        content-align: left middle;
        color: #8ac0ff;
        margin: 1 0 0 0;
    }
    #setup-help {
        width: 100%;
        content-align: left middle;
        color: #95a7ce;
        margin: 1 0 0 0;
    }
    #setup-error {
        width: 100%;
        content-align: left middle;
        color: #ff99aa;
        margin: 1 0 0 0;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "cancel_setup", "Cancel", priority=True),
        Binding("escape", "cancel_setup", "Cancel", priority=True),
    ]

    def __init__(
        self,
        *,
        login_operation: LoginOperation,
        list_projects_operation: ListProjectsOperation,
        create_project_operation: CreateProjectOperation,
        activate_project_operation: ActivateProjectOperation,
        has_active_api_key_operation: HasActiveApiKeyOperation,
        create_api_key_operation: CreateApiKeyOperation,
        has_llm_proxy_access_operation: HasLlmProxyAccessOperation | None = None,
        active_project_id: str | None = None,
        has_authenticated_session: bool = False,
        authenticated_user_name: str | None = None,
        has_codex_cli: bool = False,
        has_claude_code_cli: bool = False,
        list_existing_codex_profiles_operation: (
            ListExistingCodexProfilesOperation | None
        ) = None,
        configure_codex_profile_operation: ConfigureCodexProfileOperation | None = None,
        replace_codex_profile_operation: ReplaceCodexProfileOperation | None = None,
        remove_codex_profile_operation: RemoveCodexProfileOperation | None = None,
        get_current_codex_default_profile_operation: (
            GetCurrentCodexDefaultProfileOperation | None
        ) = None,
        configure_codex_default_profile_operation: (
            ConfigureCodexDefaultProfileOperation | None
        ) = None,
        configure_claude_operation: ConfigureClaudeOperation | None = None,
        inspect_claude_configuration_operation: (
            InspectClaudeConfigurationOperation | None
        ) = None,
        clear_claude_operation: ClearClaudeOperation | None = None,
        default_codex_profile_name: str = "meshagent",
    ) -> None:
        super().__init__()
        self._login_operation = login_operation
        self._list_projects_operation = list_projects_operation
        self._create_project_operation = create_project_operation
        self._activate_project_operation = activate_project_operation
        self._has_active_api_key_operation = has_active_api_key_operation
        self._create_api_key_operation = create_api_key_operation
        self._has_llm_proxy_access_operation = has_llm_proxy_access_operation
        self._active_project_id = active_project_id
        self._has_authenticated_session = has_authenticated_session
        self._authenticated_user_name = authenticated_user_name
        self._has_codex_cli = has_codex_cli
        self._has_claude_code_cli = has_claude_code_cli
        self._list_existing_codex_profiles_operation = (
            list_existing_codex_profiles_operation
        )
        self._configure_codex_profile_operation = configure_codex_profile_operation
        self._replace_codex_profile_operation = replace_codex_profile_operation
        self._remove_codex_profile_operation = remove_codex_profile_operation
        self._get_current_codex_default_profile_operation = (
            get_current_codex_default_profile_operation
        )
        self._configure_codex_default_profile_operation = (
            configure_codex_default_profile_operation
        )
        self._configure_claude_operation = configure_claude_operation
        self._inspect_claude_configuration_operation = (
            inspect_claude_configuration_operation
        )
        self._clear_claude_operation = clear_claude_operation
        self._default_codex_profile_name = default_codex_profile_name

        self._mode: SetupMode = "intro"
        self._projects: list[SetupProject] = []
        self._selected_project_id: str | None = None
        self._existing_codex_profile_ids: list[str] = []
        self._continued_with_existing_codex_profiles = False
        self._configured_codex_profile_id: str | None = None
        self._updated_codex_profile_ids: list[str] = []
        self._removed_codex_profile_ids: list[str] = []
        self._pending_codex_conflict_profile_id: str | None = None
        self._pending_codex_conflict_project_id: str | None = None
        self._codex_profile_scan_error: str | None = None
        self._can_use_llm_proxy: bool | None = None
        self._current_codex_default_profile_id: str | None = None
        self._configured_codex_default_profile_id: str | None = None
        self._cleared_codex_default_profile = False
        self._configured_claude = False
        self._cleared_claude = False
        self._has_existing_claude_configuration = False
        self._existing_claude_project_id: str | None = None
        self._status_lines: list[str] = []
        self._auth_url: str | None = None

        self._logo_view: Static | None = None
        self._title_view: Static | None = None
        self._message_view: Static | None = None
        self._options_view: OptionList | None = None
        self._input_view: Input | None = None
        self._status_view: Static | None = None
        self._url_view: Static | None = None
        self._help_view: Static | None = None
        self._error_view: Static | None = None

        self._logo_variant: SetupSplashVariant | None = None
        self._logo_frames: tuple[str, ...] = ()
        self._logo_frame_interval_seconds = 0.1
        self._logo_frame_index = 0

        self._status_queue: asyncio.Queue[str] = asyncio.Queue()
        self._status_consumer_task: asyncio.Task[None] | None = None
        self._login_task: asyncio.Task[None] | None = None
        self._login_watch_task: asyncio.Task[None] | None = None
        self._logo_dissolve_task: asyncio.Task[None] | None = None

        self.result = SetupWizardResult(status="canceled", message="Setup canceled.")

    def compose(self) -> ComposeResult:
        yield Static("", id="setup-logo")
        yield Static("MeshAgent Setup", id="setup-title")
        yield Static("", id="setup-message")
        yield OptionList(id="setup-options")
        yield Input(id="setup-input", placeholder="")
        yield Static("", id="setup-status")
        yield Static("", id="setup-url")
        yield Static("", id="setup-help")
        yield Static("", id="setup-error")

    async def on_mount(self) -> None:
        self._logo_view = self.query_one("#setup-logo", Static)
        self._title_view = self.query_one("#setup-title", Static)
        self._message_view = self.query_one("#setup-message", Static)
        self._options_view = self.query_one("#setup-options", OptionList)
        self._input_view = self.query_one("#setup-input", Input)
        self._status_view = self.query_one("#setup-status", Static)
        self._url_view = self.query_one("#setup-url", Static)
        self._help_view = self.query_one("#setup-help", Static)
        self._error_view = self.query_one("#setup-error", Static)

        self._update_logo_variant_for_viewport()
        self._hide_options()
        self._hide_input()
        self._hide_status()
        self._hide_url()
        self._clear_error()
        self._set_text(
            title="MeshAgent Setup",
            message="Loading...",
            help_text="Press Esc or Ctrl+C to cancel.",
        )
        if self._has_authenticated_session:
            self._show_account_choice()
        else:
            self._show_login_choice()
        self._logo_dissolve_task = asyncio.create_task(self._run_logo_dissolve_in())
        self._status_consumer_task = asyncio.create_task(self._consume_status_updates())

    def on_resize(self, event: events.Resize) -> None:
        del event
        self._update_logo_variant_for_viewport()

    async def on_unmount(self) -> None:
        current_task = asyncio.current_task()

        if (
            self._logo_dissolve_task is not None
            and self._logo_dissolve_task is not current_task
            and not self._logo_dissolve_task.done()
        ):
            self._logo_dissolve_task.cancel()
        self._logo_dissolve_task = None

        if (
            self._status_consumer_task is not None
            and self._status_consumer_task is not current_task
            and not self._status_consumer_task.done()
        ):
            self._status_consumer_task.cancel()
        self._status_consumer_task = None

        if (
            self._login_watch_task is not None
            and self._login_watch_task is not current_task
            and not self._login_watch_task.done()
        ):
            self._login_watch_task.cancel()
        self._login_watch_task = None

        if (
            self._login_task is not None
            and self._login_task is not current_task
            and not self._login_task.done()
        ):
            self._login_task.cancel()
        self._login_task = None

    async def action_cancel_setup(self) -> None:
        if self._mode == "done":
            return

        self.result = SetupWizardResult(status="canceled", message="Setup canceled.")
        if self._login_task is not None and not self._login_task.done():
            self._login_task.cancel()
        self.exit()

    async def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        selected_id = event.option.id
        if not isinstance(selected_id, str):
            return

        if self._mode == "login_choice":
            if selected_id == LOGIN_LAUNCH_OPTION_ID:
                await self._start_login()
                return
            if selected_id == LOGIN_EXIT_OPTION_ID:
                self.result = SetupWizardResult(
                    status="canceled", message="Login canceled. Exiting."
                )
                self.exit()
                return

        if self._mode == "account_choice":
            if selected_id == ACCOUNT_CONTINUE_OPTION_ID:
                await self._load_projects()
                return
            if selected_id == ACCOUNT_SWITCH_OPTION_ID:
                await self._start_login()
                return
            if selected_id == ACCOUNT_EXIT_OPTION_ID:
                self.result = SetupWizardResult(
                    status="canceled", message="Setup canceled."
                )
                self.exit()
                return

        if self._mode == "projects":
            if selected_id == PROJECT_CREATE_OPTION_ID:
                self._set_mode_project_name()
                return
            if selected_id == PROJECT_EXIT_OPTION_ID:
                self.result = SetupWizardResult(
                    status="canceled",
                    message="You chose to not activate a project. Exiting.",
                )
                self.exit()
                return
            await self._activate_project(selected_id)
            return

        if self._mode == "api_key_choice":
            if selected_id == API_KEY_SKIP_OPTION_ID:
                await self._maybe_continue_to_codex_setup()
                return
            if selected_id == API_KEY_CREATE_OPTION_ID:
                self._set_mode_api_key_name()
                return

        if self._mode == "codex_choice":
            if selected_id == CODEX_SKIP_OPTION_ID:
                await self._maybe_continue_to_claude_setup()
                return
            if selected_id == CODEX_CREATE_OPTION_ID:
                await self._configure_codex_default_profile(
                    self._default_codex_profile_name
                )
                return
            if selected_id == CODEX_CONTINUE_OPTION_ID:
                await self._continue_with_existing_codex_profiles()
                return
            if selected_id == CODEX_UPDATE_OPTION_ID:
                await self._update_existing_codex_profiles()
                return
            if selected_id == CODEX_REMOVE_OPTION_ID:
                await self._remove_existing_codex_profiles()
                return

        if self._mode == "codex_profile_conflict":
            if selected_id == CODEX_CONFLICT_UPDATE_OPTION_ID:
                await self._replace_conflicting_codex_profile()
                return
            if selected_id == CODEX_CONFLICT_REMOVE_OPTION_ID:
                await self._remove_conflicting_codex_profile()
                return
            if selected_id == CODEX_CONFLICT_CANCEL_OPTION_ID:
                self._pending_codex_conflict_profile_id = None
                self._pending_codex_conflict_project_id = None
                self._show_codex_choice()
                return

        if self._mode == "codex_default_choice":
            if selected_id == CODEX_DEFAULT_NONE_OPTION_ID:
                await self._configure_codex_default_profile(None)
                return

            default_profile_id = _codex_default_profile_id_from_option_id(selected_id)
            if default_profile_id is not None:
                await self._configure_codex_default_profile(default_profile_id)
                return

        if self._mode == "claude_choice":
            if selected_id == CLAUDE_SKIP_OPTION_ID:
                self._show_sample_choice()
                return
            if selected_id == CLAUDE_CONFIGURE_OPTION_ID:
                await self._configure_claude()
                return
            if selected_id == CLAUDE_REMOVE_OPTION_ID:
                await self._clear_claude()
                return

        if self._mode == "sample_choice":
            if selected_id == SAMPLE_CREATE_OPTION_ID:
                await self._finish_success(create_sample=True)
                return
            if selected_id == SAMPLE_SKIP_OPTION_ID:
                await self._finish_success(create_sample=False)
                return

        if self._mode == "error" and selected_id == ERROR_EXIT_OPTION_ID:
            self.exit()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        entered_value = event.value.strip()
        if self._mode == "codex_profile_name" and entered_value == "":
            entered_value = self._default_codex_profile_name
        elif entered_value == "":
            self._set_error_text("Value cannot be empty.")
            return

        if self._mode == "project_name":
            await self._create_project(entered_value)
            return

        if self._mode == "api_key_name":
            if self._selected_project_id is None:
                await self._set_error_mode("No project was selected. Exiting.")
                return
            await self._create_api_key(self._selected_project_id, entered_value)
            return

        if self._mode == "codex_profile_name":
            await self._create_codex_profile(entered_value)

    def _show_login_choice(self) -> None:
        self._mode = "login_choice"
        self._clear_error()
        self._hide_input()
        self._hide_status()
        self._hide_url()
        self._set_text(
            title="Sign in to MeshAgent",
            message="Authenticate with your MeshAgent account to continue setup.",
            help_text="Choose an option. Esc or Ctrl+C cancels.",
            centered=True,
        )
        self._set_options(
            options=[
                Option("Launch browser to sign in", id=LOGIN_LAUNCH_OPTION_ID),
                Option("Exit setup", id=LOGIN_EXIT_OPTION_ID),
            ]
        )

    def _show_account_choice(self) -> None:
        self._mode = "account_choice"
        self._clear_error()
        self._hide_input()
        self._hide_status()
        self._hide_url()

        continue_label = "Continue with current account"
        message = "You're already signed in. Continue with the current account or switch accounts."
        if (
            self._authenticated_user_name is not None
            and self._authenticated_user_name.strip() != ""
        ):
            resolved_user_name = self._authenticated_user_name.strip()
            continue_label = f"Continue as {resolved_user_name}"
            message = (
                f"You're already signed in. Continue as {resolved_user_name} "
                "or switch accounts."
            )

        self._set_text(
            title="Use Current Account",
            message=message,
            help_text="Choose an option. Esc or Ctrl+C cancels.",
            centered=True,
        )
        self._set_options(
            options=[
                Option(continue_label, id=ACCOUNT_CONTINUE_OPTION_ID),
                Option("Switch accounts", id=ACCOUNT_SWITCH_OPTION_ID),
                Option("Exit setup", id=ACCOUNT_EXIT_OPTION_ID),
            ]
        )

    async def _start_login(self) -> None:
        if self._login_task is not None and not self._login_task.done():
            return

        self._mode = "login_progress"
        self._clear_error()
        self._hide_options()
        self._hide_input()
        self._show_status()
        self._show_url(default_text="Auth URL will appear here after browser launch.")
        self._set_text(
            title="Authenticating",
            message="Complete sign-in in your browser.",
            help_text="Press Esc or Ctrl+C to cancel.",
        )
        self._append_status("Preparing browser sign-in flow...")

        self._login_task = asyncio.create_task(
            self._login_operation(self._emit_login_status)
        )
        self._login_watch_task = asyncio.create_task(self._watch_login_completion())

    async def _watch_login_completion(self) -> None:
        if self._login_task is None:
            return
        try:
            await self._login_task
        except asyncio.CancelledError:
            return
        except Exception as ex:
            await self._set_error_mode(f"Login failed: {ex}")
            return

        await self._load_projects()

    async def _load_projects(self) -> None:
        self._set_busy(
            title="Loading Projects",
            message="Fetching projects from your account...",
            help_text="Please wait.",
        )
        try:
            projects = list(await self._list_projects_operation())
        except Exception as ex:
            await self._set_error_mode(f"Unable to load projects: {ex}")
            return

        self._projects = projects
        self._show_project_selection()

    def _show_project_selection(self) -> None:
        self._mode = "projects"
        self._clear_error()
        self._hide_input()
        self._hide_status()
        self._hide_url()

        options: list[Option] = []
        if len(self._projects) == 0:
            options.append(Option("No projects available yet.", disabled=True))
        else:
            for project in self._projects:
                label = f"{project.name} ({project.id})"
                if (
                    self._active_project_id is not None
                    and project.id == self._active_project_id
                ):
                    label = f"{label} (active)"
                options.append(Option(label, id=project.id))

        options.append(Option("Create a new project", id=PROJECT_CREATE_OPTION_ID))
        options.append(Option("Exit setup", id=PROJECT_EXIT_OPTION_ID))

        message = "Choose a project to activate for CLI commands."
        if len(self._projects) == 0:
            message = "No projects found yet. Choose Create to continue."

        self._set_text(
            title="Activate a Project",
            message=message,
            help_text="Use Up/Down and Enter.",
        )
        self._set_options(options=options)

    def _set_mode_project_name(self) -> None:
        self._mode = "project_name"
        self._clear_error()
        self._hide_options()
        self._hide_status()
        self._hide_url()
        self._set_text(
            title="Create a Project",
            message="Enter a name for your new project.",
            help_text="Press Enter to continue.",
        )
        self._show_input(placeholder="my-project")

    async def _create_project(self, project_name: str) -> None:
        self._set_busy(
            title="Creating Project",
            message=f"Creating project '{project_name}'...",
            help_text="Please wait.",
        )
        try:
            project_id = await self._create_project_operation(project_name)
        except Exception as ex:
            await self._set_error_mode(f"Unable to create a project: {ex}")
            return

        await self._activate_project(project_id)

    async def _activate_project(self, project_id: str) -> None:
        self._set_busy(
            title="Activating Project",
            message=f"Activating project {project_id}...",
            help_text="Please wait.",
        )
        try:
            activated_project_id = await self._activate_project_operation(project_id)
        except Exception as ex:
            await self._set_error_mode(f"Unable to activate selected project: {ex}")
            return

        self._selected_project_id = activated_project_id
        self._can_use_llm_proxy = None

        try:
            has_active_key = await self._has_active_api_key_operation(
                activated_project_id
            )
        except Exception as ex:
            await self._set_error_mode(f"Unable to check active API key: {ex}")
            return

        if has_active_key:
            await self._maybe_continue_to_codex_setup()
            return

        self._show_api_key_choice(activated_project_id)

    def _show_api_key_choice(self, project_id: str) -> None:
        self._mode = "api_key_choice"
        self._clear_error()
        self._hide_input()
        self._hide_status()
        self._hide_url()
        self._set_text(
            title="API Key Setup",
            message=(
                f"Project {project_id} has no active API key. "
                "Create and activate one now?"
            ),
            help_text="Use Up/Down and Enter.",
        )
        self._set_options(
            options=[
                Option("Create and activate API key", id=API_KEY_CREATE_OPTION_ID),
                Option("Skip for now", id=API_KEY_SKIP_OPTION_ID),
            ]
        )

    def _set_mode_api_key_name(self) -> None:
        self._mode = "api_key_name"
        self._clear_error()
        self._hide_options()
        self._hide_status()
        self._hide_url()
        self._set_text(
            title="API Key Name",
            message="Enter a unique name for the new API key.",
            help_text="Press Enter to continue.",
        )
        self._show_input(placeholder="my-api-key")

    def _show_codex_choice(self) -> None:
        self._mode = "codex_choice"
        self._clear_error()
        self._hide_input()
        self._hide_status()
        self._hide_url()
        if self._can_use_llm_proxy is False:
            self._set_text(
                title="Codex Setup",
                message=_tool_proxy_access_required_message(tool_name="Codex"),
                help_text="Use Up/Down and Enter.",
            )
            self._set_options(
                options=[Option("Continue setup", id=CODEX_SKIP_OPTION_ID)]
            )
            return
        if len(self._existing_codex_profile_ids) == 0:
            self._set_text(
                title="Codex Setup",
                message=_tool_proxy_setup_message(tool_name="Codex"),
                help_text="Use Up/Down and Enter.",
            )
            self._set_options(
                options=[
                    Option(
                        _tool_proxy_affirmative_option_label(tool_name="Codex"),
                        id=CODEX_CREATE_OPTION_ID,
                    ),
                    Option(
                        _tool_proxy_skip_option_label(
                            tool_name="Codex",
                            launch_command="codex",
                        ),
                        id=CODEX_SKIP_OPTION_ID,
                    ),
                ],
                highlighted_id=CODEX_CREATE_OPTION_ID,
            )
        else:
            profile_message = (
                f"{_tool_proxy_setup_message(tool_name='Codex')} Found existing "
                "MeshAgent Codex configuration for this project. Use it as-is, "
                "update it for the current MeshAgent setup, configure MeshAgent "
                "as the default, or remove it."
            )

            self._set_text(
                title="Codex Setup",
                message=profile_message,
                help_text="Use Up/Down and Enter.",
            )
            self._set_options(
                options=[
                    Option(
                        "Use existing Codex configuration",
                        id=CODEX_CONTINUE_OPTION_ID,
                    ),
                    Option(
                        "Update existing Codex configuration",
                        id=CODEX_UPDATE_OPTION_ID,
                    ),
                    Option(
                        "Make MeshAgent the Codex default",
                        id=CODEX_CREATE_OPTION_ID,
                    ),
                    Option(
                        "Remove existing Codex configuration",
                        id=CODEX_REMOVE_OPTION_ID,
                    ),
                    Option(
                        _tool_proxy_skip_option_label(
                            tool_name="Codex",
                            launch_command="codex",
                        ),
                        id=CODEX_SKIP_OPTION_ID,
                    ),
                ],
                highlighted_id=CODEX_UPDATE_OPTION_ID,
            )

        if self._codex_profile_scan_error is not None:
            self._set_error_text(self._codex_profile_scan_error)

    def _set_mode_codex_profile_name(self, *, initial_value: str | None = None) -> None:
        self._mode = "codex_profile_name"
        self._clear_error()
        self._hide_options()
        self._hide_status()
        self._hide_url()
        self._set_text(
            title="Codex Configuration Name",
            message="Enter the Codex configuration name to create.",
            help_text="Press Enter to continue.",
        )
        self._show_input(
            placeholder=self._default_codex_profile_name,
            value=initial_value or self._default_codex_profile_name,
        )

    def _show_codex_profile_conflict(
        self,
        *,
        profile_id: str,
        project_id: str | None,
    ) -> None:
        self._mode = "codex_profile_conflict"
        self._pending_codex_conflict_profile_id = profile_id
        self._pending_codex_conflict_project_id = project_id
        self._clear_error()
        self._hide_input()
        self._hide_status()
        self._hide_url()

        message = (
            f"Codex configuration {profile_id} is already configured for another "
            "MeshAgent project. Update it to use the current project, remove it, "
            "or go back to choose a different configuration name."
        )
        if project_id is not None and project_id.strip() != "":
            message = (
                f"Codex configuration {profile_id} is currently configured for "
                f"MeshAgent project {project_id}. Update it to use the current "
                "project, remove it, or go back to choose a different configuration "
                "name."
            )

        self._set_text(
            title="Codex Configuration Conflict",
            message=message,
            help_text="Use Up/Down and Enter.",
        )
        self._set_options(
            options=[
                Option(
                    f"Update {profile_id} to use the current project",
                    id=CODEX_CONFLICT_UPDATE_OPTION_ID,
                ),
                Option(
                    f"Remove {profile_id}",
                    id=CODEX_CONFLICT_REMOVE_OPTION_ID,
                ),
                Option("Go back", id=CODEX_CONFLICT_CANCEL_OPTION_ID),
            ],
            highlighted_id=CODEX_CONFLICT_UPDATE_OPTION_ID,
        )

    def _show_codex_default_choice(self, *, profile_ids: Sequence[str]) -> None:
        self._mode = "codex_default_choice"
        self._clear_error()
        self._hide_input()
        self._hide_status()
        self._hide_url()

        options: list[Option] = []
        if len(profile_ids) == 1:
            profile_id = profile_ids[0]
            self._set_text(
                title="Codex Default",
                message="Make MeshAgent the default Codex provider?",
                help_text="Use Up/Down and Enter.",
            )
            options.append(
                Option(
                    "Yes, use MeshAgent as the Codex default",
                    id=_codex_default_profile_option_id(profile_id),
                )
            )
        else:
            self._set_text(
                title="Codex Default",
                message="Choose which MeshAgent Codex configuration should be the default provider.",
                help_text="Use Up/Down and Enter.",
            )
            for profile_id in profile_ids:
                options.append(
                    Option(
                        f"Make {profile_id} the default provider",
                        id=_codex_default_profile_option_id(profile_id),
                    )
                )

        options.append(
            Option(
                _tool_proxy_skip_option_label(
                    tool_name="Codex",
                    launch_command="codex",
                ),
                id=CODEX_DEFAULT_NONE_OPTION_ID,
            )
        )

        highlighted_id = CODEX_DEFAULT_NONE_OPTION_ID
        if self._current_codex_default_profile_id in profile_ids:
            highlighted_id = _codex_default_profile_option_id(
                self._current_codex_default_profile_id
            )

        self._set_options(options=options, highlighted_id=highlighted_id)

    def _show_claude_choice(self) -> None:
        self._mode = "claude_choice"
        self._clear_error()
        self._hide_input()
        self._hide_status()
        self._hide_url()
        if self._can_use_llm_proxy is False:
            self._set_text(
                title="Claude Setup",
                message=_tool_proxy_access_required_message(tool_name="Claude"),
                help_text="Use Up/Down and Enter.",
            )
            self._set_options(
                options=[Option("Finish setup", id=CLAUDE_SKIP_OPTION_ID)]
            )
            return
        message = _tool_proxy_setup_message(tool_name="Claude")
        options = [
            Option(
                _tool_proxy_affirmative_option_label(tool_name="Claude"),
                id=CLAUDE_CONFIGURE_OPTION_ID,
            ),
            Option(
                _tool_proxy_skip_option_label(
                    tool_name="Claude",
                    launch_command="claude",
                ),
                id=CLAUDE_SKIP_OPTION_ID,
            ),
        ]
        highlighted_id = CLAUDE_CONFIGURE_OPTION_ID

        if self._has_existing_claude_configuration:
            message = (
                f"{_tool_proxy_setup_message(tool_name='Claude')} Found an existing "
                "MeshAgent Claude configuration on this machine."
            )
            if (
                self._existing_claude_project_id is not None
                and self._existing_claude_project_id.strip() != ""
            ):
                message = (
                    f"{message} It is currently configured for project "
                    f"{self._existing_claude_project_id}."
                )
            message = (
                f"{message} Update it for the current project, remove it, or leave "
                "Claude unchanged."
            )
            options = [
                Option(
                    "Update Claude MeshAgent configuration",
                    id=CLAUDE_CONFIGURE_OPTION_ID,
                ),
                Option(
                    "Remove Claude MeshAgent configuration",
                    id=CLAUDE_REMOVE_OPTION_ID,
                ),
                Option(
                    _tool_proxy_skip_option_label(
                        tool_name="Claude",
                        launch_command="claude",
                    ),
                    id=CLAUDE_SKIP_OPTION_ID,
                ),
            ]

        self._set_text(
            title="Claude Setup",
            message=message,
            help_text="Use Up/Down and Enter.",
        )
        self._set_options(
            options=options,
            highlighted_id=highlighted_id,
        )

    def _show_sample_choice(self) -> None:
        self._mode = "sample_choice"
        self._clear_error()
        self._hide_input()
        self._hide_status()
        self._hide_url()
        self._set_text(
            title="Create Sample App",
            message="Would you like to create a sample MeshAgent application now?",
            help_text="Use Up/Down and Enter.",
        )
        self._set_options(
            options=[
                Option("Create a sample application", id=SAMPLE_CREATE_OPTION_ID),
                Option("Skip for now", id=SAMPLE_SKIP_OPTION_ID),
            ],
            highlighted_id=SAMPLE_CREATE_OPTION_ID,
        )

    async def _create_api_key(self, project_id: str, api_key_name: str) -> None:
        self._set_busy(
            title="Creating API Key",
            message=f"Creating API key '{api_key_name}'...",
            help_text="Please wait.",
        )
        try:
            await self._create_api_key_operation(project_id, api_key_name)
        except Exception as ex:
            await self._set_error_mode(f"Unable to create API key: {ex}")
            return

        await self._maybe_continue_to_codex_setup()

    async def _ensure_llm_proxy_access_checked(self) -> bool:
        if self._can_use_llm_proxy is not None:
            return self._can_use_llm_proxy

        if (
            self._selected_project_id is None
            or self._has_llm_proxy_access_operation is None
        ):
            self._can_use_llm_proxy = True
            return True

        self._set_busy(
            title="Checking LLM Proxy Access",
            message="Checking whether your MeshAgent account can use the LLM proxy for this project...",
            help_text="Please wait.",
        )
        try:
            self._can_use_llm_proxy = await self._has_llm_proxy_access_operation(
                self._selected_project_id
            )
        except Exception as ex:
            await self._set_error_mode(f"Unable to check LLM proxy access: {ex}")
            return False

        return self._can_use_llm_proxy

    async def _maybe_continue_to_codex_setup(self) -> None:
        if not self._has_codex_cli or self._configure_codex_profile_operation is None:
            await self._maybe_continue_to_claude_setup()
            return

        self._continued_with_existing_codex_profiles = False
        self._existing_codex_profile_ids = []
        self._updated_codex_profile_ids = []
        self._removed_codex_profile_ids = []
        self._pending_codex_conflict_profile_id = None
        self._pending_codex_conflict_project_id = None
        self._codex_profile_scan_error = None
        self._current_codex_default_profile_id = None
        self._configured_codex_default_profile_id = None
        self._cleared_codex_default_profile = False
        can_use_llm_proxy = await self._ensure_llm_proxy_access_checked()
        if self._mode == "error":
            return
        if not can_use_llm_proxy:
            self._show_codex_choice()
            return
        if (
            self._selected_project_id is not None
            and self._list_existing_codex_profiles_operation is not None
        ):
            self._set_busy(
                title="Checking Codex",
                message="Inspecting the existing Codex configuration...",
                help_text="Please wait.",
            )
            try:
                existing_profile_ids = list(
                    await self._list_existing_codex_profiles_operation(
                        self._selected_project_id
                    )
                )
            except Exception as ex:
                self._codex_profile_scan_error = (
                    f"Unable to inspect existing Codex configurations: {ex}"
                )
            else:
                self._existing_codex_profile_ids = existing_profile_ids

        self._show_codex_choice()

    async def _create_codex_profile(self, profile_name: str) -> None:
        if self._configure_codex_profile_operation is None:
            await self._maybe_continue_to_claude_setup()
            return

        self._set_busy(
            title="Configuring Codex",
            message=f"Creating Codex configuration '{profile_name}'...",
            help_text="Please wait.",
        )
        try:
            await self._configure_codex_profile_operation(profile_name)
        except CodexProfileConflictError as ex:
            self._show_codex_profile_conflict(
                profile_id=ex.profile_id,
                project_id=ex.project_id,
            )
            return
        except ValueError as ex:
            await self._set_error_mode(f"Unable to configure Codex: {ex}")
            return
        except Exception as ex:
            await self._set_error_mode(f"Unable to configure Codex: {ex}")
            return

        self._configured_codex_profile_id = profile_name
        await self._maybe_continue_to_codex_default_choice([profile_name])

    async def _continue_with_existing_codex_profiles(self) -> None:
        self._continued_with_existing_codex_profiles = True
        await self._maybe_continue_to_codex_default_choice(
            self._existing_codex_profile_ids
        )

    async def _update_existing_codex_profiles(self) -> None:
        if (
            self._replace_codex_profile_operation is None
            or len(self._existing_codex_profile_ids) == 0
        ):
            await self._continue_with_existing_codex_profiles()
            return

        self._set_busy(
            title="Updating Codex",
            message="Updating existing Codex configurations for the current project...",
            help_text="Please wait.",
        )
        updated_profile_ids: list[str] = []
        try:
            for profile_id in self._existing_codex_profile_ids:
                await self._replace_codex_profile_operation(profile_id)
                updated_profile_ids.append(profile_id)
        except Exception as ex:
            await self._set_error_mode(f"Unable to update Codex: {ex}")
            return

        self._updated_codex_profile_ids = updated_profile_ids
        await self._maybe_continue_to_codex_default_choice(
            self._existing_codex_profile_ids
        )

    async def _remove_existing_codex_profiles(self) -> None:
        if (
            self._remove_codex_profile_operation is None
            or len(self._existing_codex_profile_ids) == 0
        ):
            self._show_codex_choice()
            return

        self._set_busy(
            title="Removing Codex Configurations",
            message="Removing existing MeshAgent Codex configurations...",
            help_text="Please wait.",
        )
        try:
            for profile_id in self._existing_codex_profile_ids:
                await self._remove_codex_profile_operation(profile_id)
        except Exception as ex:
            await self._set_error_mode(f"Unable to remove Codex configurations: {ex}")
            return

        self._removed_codex_profile_ids = list(self._existing_codex_profile_ids)
        self._existing_codex_profile_ids = []
        self._show_codex_choice()

    async def _replace_conflicting_codex_profile(self) -> None:
        profile_id = self._pending_codex_conflict_profile_id
        if profile_id is None or self._replace_codex_profile_operation is None:
            self._show_codex_choice()
            return

        self._set_busy(
            title="Updating Codex",
            message=f"Updating Codex configuration '{profile_id}'...",
            help_text="Please wait.",
        )
        try:
            await self._replace_codex_profile_operation(profile_id)
        except Exception as ex:
            await self._set_error_mode(f"Unable to update Codex: {ex}")
            return

        self._updated_codex_profile_ids = [profile_id]
        self._pending_codex_conflict_profile_id = None
        self._pending_codex_conflict_project_id = None
        await self._maybe_continue_to_codex_default_choice([profile_id])

    async def _remove_conflicting_codex_profile(self) -> None:
        profile_id = self._pending_codex_conflict_profile_id
        if profile_id is None or self._remove_codex_profile_operation is None:
            self._show_codex_choice()
            return

        self._set_busy(
            title="Removing Codex Configuration",
            message=f"Removing Codex configuration '{profile_id}'...",
            help_text="Please wait.",
        )
        try:
            await self._remove_codex_profile_operation(profile_id)
        except Exception as ex:
            await self._set_error_mode(f"Unable to remove Codex configuration: {ex}")
            return

        self._removed_codex_profile_ids.append(profile_id)
        self._pending_codex_conflict_profile_id = None
        self._pending_codex_conflict_project_id = None
        await self._configure_codex_default_profile(self._default_codex_profile_name)

    async def _maybe_continue_to_codex_default_choice(
        self,
        profile_ids: Sequence[str],
    ) -> None:
        if (
            self._selected_project_id is None
            or len(profile_ids) == 0
            or self._configure_codex_default_profile_operation is None
        ):
            await self._maybe_continue_to_claude_setup()
            return

        self._current_codex_default_profile_id = None
        if self._get_current_codex_default_profile_operation is not None:
            self._set_busy(
                title="Checking Codex Default",
                message="Inspecting the current Codex default provider...",
                help_text="Please wait.",
            )
            try:
                self._current_codex_default_profile_id = (
                    await self._get_current_codex_default_profile_operation(
                        self._selected_project_id
                    )
                )
            except Exception as ex:
                await self._set_error_mode(
                    f"Unable to inspect the Codex default provider: {ex}"
                )
                return

        self._show_codex_default_choice(profile_ids=profile_ids)

    async def _configure_codex_default_profile(self, profile_id: str | None) -> None:
        if (
            self._selected_project_id is None
            or self._configure_codex_default_profile_operation is None
        ):
            await self._maybe_continue_to_claude_setup()
            return

        message = "Resetting Codex to the OpenAI default provider..."
        if profile_id is not None:
            message = "Configuring Codex to use MeshAgent by default..."

        self._set_busy(
            title="Configuring Codex Default",
            message=message,
            help_text="Please wait.",
        )
        try:
            await self._configure_codex_default_profile_operation(
                self._selected_project_id,
                profile_id,
            )
        except Exception as ex:
            await self._set_error_mode(f"Unable to configure the Codex default: {ex}")
            return

        self._configured_codex_default_profile_id = profile_id
        self._cleared_codex_default_profile = profile_id is None
        await self._maybe_continue_to_claude_setup()

    async def _maybe_continue_to_claude_setup(self) -> None:
        if not self._has_claude_code_cli or self._configure_claude_operation is None:
            self._show_sample_choice()
            return

        await self._ensure_llm_proxy_access_checked()
        if self._mode == "error":
            return
        self._has_existing_claude_configuration = False
        self._existing_claude_project_id = None
        if self._inspect_claude_configuration_operation is not None:
            self._set_busy(
                title="Checking Claude",
                message="Inspecting the existing Claude configuration...",
                help_text="Please wait.",
            )
            try:
                status = await self._inspect_claude_configuration_operation()
            except Exception as ex:
                await self._set_error_mode(
                    f"Unable to inspect the Claude configuration: {ex}"
                )
                return
            self._has_existing_claude_configuration = status.configured
            self._existing_claude_project_id = status.project_id
        self._show_claude_choice()

    async def _configure_claude(self) -> None:
        if (
            self._selected_project_id is None
            or self._configure_claude_operation is None
        ):
            self._show_sample_choice()
            return

        self._set_busy(
            title="Configuring Claude",
            message="Configuring Claude to use MeshAgent by default...",
            help_text="Please wait.",
        )
        try:
            await self._configure_claude_operation(self._selected_project_id)
        except Exception as ex:
            await self._set_error_mode(f"Unable to configure Claude: {ex}")
            return

        self._configured_claude = True
        self._show_sample_choice()

    async def _clear_claude(self) -> None:
        if self._clear_claude_operation is None:
            self._show_sample_choice()
            return

        self._set_busy(
            title="Removing Claude Configuration",
            message="Removing the MeshAgent Claude configuration...",
            help_text="Please wait.",
        )
        try:
            await self._clear_claude_operation()
        except Exception as ex:
            await self._set_error_mode(f"Unable to remove Claude configuration: {ex}")
            return

        self._cleared_claude = True
        self._show_sample_choice()

    async def _finish_success(self, *, create_sample: bool = False) -> None:
        self._mode = "done"
        await self._stop_logo_dissolve()
        self._clear_error()
        self._hide_options()
        self._hide_input()
        self._hide_status()
        self._hide_url()
        message = "Project activated and setup finished."
        if self._configured_codex_profile_id is not None:
            message = (
                "Project activated and Codex configuration "
                f"{self._configured_codex_profile_id} created."
            )
        elif len(self._updated_codex_profile_ids) > 0:
            message = "Project activated and existing Codex configuration was updated."
        elif self._continued_with_existing_codex_profiles:
            message = (
                "Project activated and existing Codex configuration is ready to use."
            )
        elif len(self._removed_codex_profile_ids) > 0:
            message = "Project activated and Codex configuration was removed."
        if self._configured_codex_default_profile_id is not None:
            message = f"{message} Codex is configured to use MeshAgent by default."
        elif self._cleared_codex_default_profile:
            message = f"{message} Codex is reset to the OpenAI default provider."
        if self._configured_claude:
            message = f"{message} Claude is configured to use MeshAgent by default."
        elif self._cleared_claude:
            message = f"{message} Claude MeshAgent configuration was removed."
        if create_sample:
            message = f"{message} Opening the sample app wizard next."
        self._set_text(
            title="Setup Complete",
            message=message,
            help_text="",
        )

        self.result = SetupWizardResult(
            status="completed",
            project_id=self._selected_project_id,
            create_sample=create_sample,
        )

        await self._run_logo_fade()
        self.exit()

    async def _set_error_mode(self, message: str) -> None:
        self._mode = "error"
        self.result = SetupWizardResult(status="error", message=message)
        self._hide_input()
        self._hide_status()
        self._hide_url()
        self._set_text(
            title="Setup Failed",
            message="An error occurred while running setup.",
            help_text="Press Enter on Exit setup to close.",
        )
        self._set_error_text(message)
        self._set_options(options=[Option("Exit setup", id=ERROR_EXIT_OPTION_ID)])

    async def _emit_login_status(self, message: str) -> None:
        await self._status_queue.put(message)

    async def _consume_status_updates(self) -> None:
        try:
            while True:
                status = await self._status_queue.get()
                auth_url = self._extract_auth_url(status)
                if auth_url is not None:
                    self._auth_url = auth_url
                    if self._url_view is not None and self._url_view.display:
                        self._url_view.update(f"Auth URL: {auth_url}")
                    self._append_status(
                        "Browser launched. Complete sign-in in your browser."
                    )
                else:
                    self._append_status(self._normalize_status_line(status))
        except asyncio.CancelledError:
            return

    async def _run_logo_fade(self) -> None:
        await asyncio.sleep(0.25)

    async def _run_logo_dissolve_in(self) -> None:
        try:
            while True:
                if (
                    self._logo_view is None
                    or not self._logo_view.display
                    or len(self._logo_frames) == 0
                ):
                    await asyncio.sleep(0.1)
                    continue
                self._render_logo()
                self._logo_frame_index = (self._logo_frame_index + 1) % len(
                    self._logo_frames
                )
                await asyncio.sleep(self._logo_frame_interval_seconds)
        except asyncio.CancelledError:
            return

    async def _stop_logo_dissolve(self) -> None:
        current_task = asyncio.current_task()
        if (
            self._logo_dissolve_task is None
            or self._logo_dissolve_task is current_task
            or self._logo_dissolve_task.done()
        ):
            return
        self._logo_dissolve_task.cancel()
        try:
            await self._logo_dissolve_task
        except asyncio.CancelledError:
            pass
        self._logo_dissolve_task = None
        self._render_logo()

    def _set_text(
        self, *, title: str, message: str, help_text: str, centered: bool = False
    ) -> None:
        self._set_header_alignment(centered=centered)
        if self._title_view is not None:
            self._title_view.update(title)
        if self._message_view is not None:
            self._message_view.update(message)
        if self._help_view is not None:
            self._help_view.update(help_text)

    def _set_header_alignment(self, *, centered: bool) -> None:
        horizontal_alignment = "center" if centered else "left"
        if self._title_view is not None:
            self._title_view.styles.content_align = (horizontal_alignment, "middle")
        if self._message_view is not None:
            self._message_view.styles.content_align = (horizontal_alignment, "middle")

    @staticmethod
    def _first_enabled_option_index(options: Sequence[Option]) -> int | None:
        for index, option in enumerate(options):
            if not option.disabled:
                return index
        return None

    def _set_options(
        self,
        *,
        options: Sequence[Option],
        highlighted_id: str | None = None,
    ) -> None:
        if self._options_view is None:
            return
        option_list = list(options)
        self._options_view.clear_options()
        self._options_view.add_options(option_list)
        highlighted_index = self._first_enabled_option_index(option_list)
        if highlighted_id is not None:
            for index, option in enumerate(option_list):
                if option.id == highlighted_id and not option.disabled:
                    highlighted_index = index
                    break
        self._options_view.highlighted = highlighted_index
        self._options_view.display = True
        self._options_view.focus()

    def _hide_options(self) -> None:
        if self._options_view is not None:
            self._options_view.display = False

    def _show_input(self, *, placeholder: str, value: str = "") -> None:
        if self._input_view is None:
            return
        self._input_view.value = value
        self._input_view.placeholder = placeholder
        self._input_view.display = True
        self._input_view.focus()

    def _hide_input(self) -> None:
        if self._input_view is not None:
            self._input_view.display = False

    def _show_status(self) -> None:
        if self._status_view is not None:
            self._status_view.display = True
            self._status_view.update("\n".join(self._status_lines[-6:]))

    def _hide_status(self) -> None:
        if self._status_view is not None:
            self._status_view.display = False

    def _show_url(self, *, default_text: str) -> None:
        if self._url_view is None:
            return
        if self._auth_url is not None:
            self._url_view.update(f"Auth URL: {self._auth_url}")
        else:
            self._url_view.update(default_text)
        self._url_view.display = True

    def _hide_url(self) -> None:
        if self._url_view is not None:
            self._url_view.display = False

    def _set_busy(self, *, title: str, message: str, help_text: str) -> None:
        self._mode = "busy"
        self._clear_error()
        self._hide_options()
        self._hide_input()
        self._hide_url()
        self._show_status()
        self._set_text(title=title, message=message, help_text=help_text)

    def _append_status(self, message: str) -> None:
        self._status_lines.append(message)
        if self._status_view is not None and self._status_view.display:
            self._status_view.update("\n".join(self._status_lines[-6:]))

    def _set_error_text(self, message: str) -> None:
        if self._error_view is not None:
            self._error_view.display = True
            self._error_view.update(message)

    def _clear_error(self) -> None:
        if self._error_view is not None:
            self._error_view.display = False
            self._error_view.update("")

    def _extract_auth_url(self, status: str) -> str | None:
        if not status.startswith(self._AUTH_URL_PREFIX):
            return None

        auth_url = status[len(self._AUTH_URL_PREFIX) :].strip()
        if auth_url.endswith("..."):
            auth_url = auth_url[:-3].strip()
        if auth_url.endswith("…"):
            auth_url = auth_url[:-1].strip()
        if auth_url == "":
            return None
        return auth_url

    def _normalize_status_line(self, status: str) -> str:
        if status.startswith("✅ "):
            return status[2:].strip()
        return status

    def _render_logo(
        self, *, fade_factor: float = 1.0, dissolve_progress: float | None = None
    ) -> None:
        del fade_factor
        del dissolve_progress

        if self._logo_view is None:
            return

        if len(self._logo_frames) == 0:
            self._logo_view.update("")
            return

        frame = self._logo_frames[self._logo_frame_index]
        self._logo_view.update(Text.from_ansi(frame))

    def _update_logo_variant_for_viewport(self) -> None:
        if self._logo_view is None:
            return

        selected_variant = self._select_logo_variant_for_viewport(
            viewport_width=self.size.width,
            viewport_height=self.size.height,
        )
        if selected_variant is None:
            self._logo_variant = None
            self._logo_frames = ()
            self._logo_frame_index = 0
            self._logo_view.display = False
            self._logo_view.update("")
            return

        if (
            self._logo_variant is None
            or self._logo_variant.name != selected_variant.name
        ):
            self._logo_frames = load_setup_splash_frames(selected_variant.name)
            self._logo_variant = selected_variant
            self._logo_frame_interval_seconds = selected_variant.frame_interval_seconds
            self._logo_frame_index = 0
        elif len(self._logo_frames) > 0:
            self._logo_frame_index %= len(self._logo_frames)

        if len(self._logo_frames) == 0:
            self._logo_variant = None
            self._logo_view.display = False
            self._logo_view.update("")
            return

        self._logo_view.display = True
        self._logo_view.styles.height = selected_variant.rows
        self._render_logo()

    def _select_logo_variant_for_viewport(
        self, *, viewport_width: int, viewport_height: int
    ) -> SetupSplashVariant | None:
        available_logo_columns = viewport_width - self._LOGO_HORIZONTAL_PADDING_COLUMNS
        available_logo_rows = viewport_height - self._LOGO_RESERVED_LOGIN_ROWS

        if (
            available_logo_columns < self._LOGO_MIN_DECENT_COLUMNS
            or available_logo_rows < self._LOGO_MIN_DECENT_ROWS
        ):
            return None

        for variant in SETUP_SPLASH_VARIANTS:
            if (
                variant.columns <= available_logo_columns
                and variant.rows <= available_logo_rows
            ):
                return variant
        return None

    def _build_logo_reveal_order(self) -> tuple[dict[tuple[int, int], int], int]:
        glyph_positions: list[tuple[int, int]] = []
        for row_index, line in enumerate(MESHAGENT_SETUP_LOGO_LINES):
            for column_index, char in enumerate(line):
                if char != " ":
                    glyph_positions.append((row_index, column_index))

        randomizer = random.Random(0x4D455348)
        randomizer.shuffle(glyph_positions)

        rank_by_position: dict[tuple[int, int], int] = {}
        for rank, position in enumerate(glyph_positions, start=1):
            rank_by_position[position] = rank

        return rank_by_position, len(glyph_positions)

    def _is_logo_glyph_revealed(
        self, *, row: int, column: int, progress: float
    ) -> bool:
        if progress <= 0.0:
            return False
        if progress >= 1.0 or self._logo_reveal_total == 0:
            return True

        reveal_count = max(1, int(progress * float(self._logo_reveal_total)))
        rank = self._logo_reveal_rank_by_position.get((row, column))
        if rank is None:
            return False
        return rank <= reveal_count

    def _style_for_glyph(self, *, row: int, column: int, fade_factor: float) -> str:
        base_red, base_green, base_blue = self._base_logo_rgb(row=row, column=column)
        if self._mode == "intro":
            shimmer_center = self._shimmer_offset - (row // 3)
            distance = abs(column - shimmer_center)
            if distance <= 1:
                highlight_red, highlight_green, highlight_blue = (230, 247, 255)
                highlight_mix = 0.8
            elif distance <= 3:
                highlight_red, highlight_green, highlight_blue = (191, 233, 255)
                highlight_mix = 0.6
            elif distance <= 6:
                highlight_red, highlight_green, highlight_blue = (219, 240, 255)
                highlight_mix = 0.35
            else:
                highlight_red, highlight_green, highlight_blue = (
                    base_red,
                    base_green,
                    base_blue,
                )
                highlight_mix = 0.0
        else:
            highlight_red, highlight_green, highlight_blue = (
                base_red,
                base_green,
                base_blue,
            )
            highlight_mix = 0.0
            distance = 99

        target_red = int(base_red + (highlight_red - base_red) * highlight_mix)
        target_green = int(base_green + (highlight_green - base_green) * highlight_mix)
        target_blue = int(base_blue + (highlight_blue - base_blue) * highlight_mix)

        background_red, background_green, background_blue = (8, 10, 14)
        blended_red = int(background_red + (target_red - background_red) * fade_factor)
        blended_green = int(
            background_green + (target_green - background_green) * fade_factor
        )
        blended_blue = int(
            background_blue + (target_blue - background_blue) * fade_factor
        )
        color = f"#{blended_red:02x}{blended_green:02x}{blended_blue:02x}"

        if distance <= 3:
            return f"bold {color}"
        return color

    def _base_logo_rgb(self, *, row: int, column: int) -> tuple[int, int, int]:
        if row < 0 or row >= len(MESHAGENT_SETUP_LOGO_COLOR_HEX_LINES):
            return (248, 250, 255)

        row_colors = MESHAGENT_SETUP_LOGO_COLOR_HEX_LINES[row]
        start = column * 7
        end = start + 7
        if start < 0 or end > len(row_colors):
            return (248, 250, 255)

        color = row_colors[start:end]
        if len(color) != 7 or not color.startswith("#"):
            return (248, 250, 255)

        return (
            int(color[1:3], 16),
            int(color[3:5], 16),
            int(color[5:7], 16),
        )


async def _run_app(app: App[None]) -> None:
    app_token = active_app.set(app)
    try:
        await app.run_async()
    finally:
        active_app.reset(app_token)


async def run_setup_wizard_tui(
    *,
    login_operation: LoginOperation,
    list_projects_operation: ListProjectsOperation,
    create_project_operation: CreateProjectOperation,
    activate_project_operation: ActivateProjectOperation,
    has_active_api_key_operation: HasActiveApiKeyOperation,
    create_api_key_operation: CreateApiKeyOperation,
    active_project_id: str | None,
    has_llm_proxy_access_operation: HasLlmProxyAccessOperation | None = None,
    has_authenticated_session: bool = False,
    authenticated_user_name: str | None = None,
    has_codex_cli: bool = False,
    has_claude_code_cli: bool = False,
    list_existing_codex_profiles_operation: (
        ListExistingCodexProfilesOperation | None
    ) = None,
    configure_codex_profile_operation: ConfigureCodexProfileOperation | None = None,
    replace_codex_profile_operation: ReplaceCodexProfileOperation | None = None,
    remove_codex_profile_operation: RemoveCodexProfileOperation | None = None,
    get_current_codex_default_profile_operation: (
        GetCurrentCodexDefaultProfileOperation | None
    ) = None,
    configure_codex_default_profile_operation: (
        ConfigureCodexDefaultProfileOperation | None
    ) = None,
    configure_claude_operation: ConfigureClaudeOperation | None = None,
    inspect_claude_configuration_operation: (
        InspectClaudeConfigurationOperation | None
    ) = None,
    clear_claude_operation: ClearClaudeOperation | None = None,
    default_codex_profile_name: str = "meshagent",
) -> SetupWizardResult:
    app = SetupWizardApp(
        login_operation=login_operation,
        list_projects_operation=list_projects_operation,
        create_project_operation=create_project_operation,
        activate_project_operation=activate_project_operation,
        has_active_api_key_operation=has_active_api_key_operation,
        create_api_key_operation=create_api_key_operation,
        has_llm_proxy_access_operation=has_llm_proxy_access_operation,
        active_project_id=active_project_id,
        has_authenticated_session=has_authenticated_session,
        authenticated_user_name=authenticated_user_name,
        has_codex_cli=has_codex_cli,
        has_claude_code_cli=has_claude_code_cli,
        list_existing_codex_profiles_operation=list_existing_codex_profiles_operation,
        configure_codex_profile_operation=configure_codex_profile_operation,
        replace_codex_profile_operation=replace_codex_profile_operation,
        remove_codex_profile_operation=remove_codex_profile_operation,
        get_current_codex_default_profile_operation=(
            get_current_codex_default_profile_operation
        ),
        configure_codex_default_profile_operation=(
            configure_codex_default_profile_operation
        ),
        configure_claude_operation=configure_claude_operation,
        inspect_claude_configuration_operation=(inspect_claude_configuration_operation),
        clear_claude_operation=clear_claude_operation,
        default_codex_profile_name=default_codex_profile_name,
    )

    try:
        await _run_app(app)
    except KeyboardInterrupt:
        return SetupWizardResult(status="canceled", message="Setup canceled.")

    return app.result
