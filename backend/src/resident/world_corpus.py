"""A bounded, hermetic "world" for the World Window (Phase 6, ADR-0008).

The World Window needs a *source of external material* it can browse. Per the
standing constraints (CLAUDE.md: "External side effects remain sandboxed and
permissioned"; tests must stay hermetic), that source is **not a live web**: it
is a fixed, in-code corpus of items, each carrying the minimal provenance a
world observation needs — a ``source`` (which "site"), a ``topic`` (what domain
it is about), a ``title``, a neutral ``summary`` (the *observed fact*, in the
third person — never the resident's viewpoint), and ``links`` (ids of related
items, which the **accident** entry mode walks, the way a real browser follows a
link).

It is deliberately finite and fixed so a run is deterministic and reproducible
(the same "world" for every seed, exactly like the deterministic fake model
provider). Its shape is chosen to make the four entry modes and the metrics they
drive actually exercisable:

- **on-topic** items overlap the simulated user's topics (food / memory / work /
  music), so the **follow** mode can reach material adjacent to a real thread;
- **edge** items sit next to those (cognition next to memory, nutrition next to
  food), so the **edge** mode has a cognitive boundary to explore;
- **alien** items are domains the user never raises (astrophysics / mycology /
  history / typography), so the **alien** mode has genuinely unfamiliar ground;
- **cross-links** connect on-topic → edge → alien, so an **accident** chain can
  realistically wander from the familiar into the foreign (the "I was reading
  about pasta, followed a link to fermentation, which linked to fungi" accident).

The summaries are written neutrally (what the material *says*), because the
phase's core invariant is **reading ≠ thought, observation ≠ belief**: the
corpus supplies the *observation*; the resident's *impression* and *thought* are
separate, grounded, low-probability steps the engine performs on top of it
(see :mod:`resident.world`).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class WorldItem:
    """One unit of external material the World Window may observe.

    ``links`` are ids of *other* corpus items (a bounded graph). An item with no
    links is a dead end (a real page with nowhere to click); the accident mode
    simply cannot continue from it.
    """

    id: str
    source: str        # which "site" published it (drives source_concentration)
    topic: str         # the domain it is about (drives topic matching + concentration)
    title: str
    summary: str       # the observed fact (neutral, third person — NOT a viewpoint)
    links: tuple[str, ...] = field(default_factory=tuple)  # related item ids

    @property
    def text(self) -> str:
        """Human-readable blob used for recall/association (mirrors Event.text)."""
        return f"{self.title}。{self.summary}"


# --------------------------------------------------------------------------- corpus
#
# Topics and their relationship to the simulated user's life:
#   on-topic : food, memory, work, music      (the user actually raises these)
#   edge     : cognition, nutrition, craft    (adjacent to on-topic)
#   alien    : astrophysics, mycology, history, typography (never raised by the user)
#
# Sources (5 "sites") are spread across topics so that adhesion to a *single*
# source is measurable and, if it happens, is a finding rather than baked in.
#
# Cross-links form a few connected components that bridge on-topic -> edge ->
# alien, so an accident walk can drift outward. Each item lists 1-3 links.

DEFAULT_WORLD_ITEMS: tuple[WorldItem, ...] = (
    # --- on-topic: food (the user's pasta thread) -----------------------------
    WorldItem(
        id="w_pasta_science", source="wiki", topic="food",
        title="面团的筋度从何而来",
        summary="面筋由麦谷蛋白与醇溶蛋白吸水交联形成，揉捏使面筋网络延展，决定意面的嚼劲。",
        links=("w_gluten_free", "w_umami"),
    ),
    WorldItem(
        id="w_umami", source="science_daily", topic="food",
        title="鲜味的化学基础",
        summary="谷氨酸与肌苷酸盐协同增强咸鲜感，番茄长时间熬煮会释放更多游离谷氨酸，让酱汁更鲜。",
        links=("w_pasta_science", "w_nutrition_gut"),
    ),
    WorldItem(
        id="w_gluten_free", source="blog", topic="food",
        title="无麸质面食的取舍",
        summary="以米粉、木薯粉替代小麦会失去面筋骨架，需加淀粉与蛋来补偿结构与口感。",
        links=("w_pasta_science", "w_nutrition_gut"),
    ),
    # --- on-topic: memory (the user's book-about-memory thread) ---------------
    WorldItem(
        id="w_memory_consolidation", source="journal", topic="memory",
        title="记忆的巩固与重激活",
        summary="睡眠中海马体会将新记忆反复重放并逐步转移到皮层；一次重激活会让旧记忆短暂可写。",
        links=("w_sleep_memory", "w_autobiographical"),
    ),
    WorldItem(
        id="w_sleep_memory", source="science_daily", topic="memory",
        title="睡眠如何塑造记忆",
        summary="慢波睡眠偏好巩固情绪与情节记忆，REM 睡眠则更多参与程序性技能与情绪再加工。",
        links=("w_memory_consolidation", "w_cognition_attention"),
    ),
    WorldItem(
        id="w_autobiographical", source="journal", topic="memory",
        title="自传体记忆的结构",
        summary="早期生活与高峰期的事件被回忆得更密集，形成所谓『生命周期图』，并带有强烈的情感标记。",
        links=("w_memory_consolidation", "w_history_oral"),
    ),
    # --- on-topic: work (the user's deadline thread) --------------------------
    WorldItem(
        id="w_deadline_psych", source="science_daily", topic="work",
        title="截止日期与拖延",
        summary="外部截止点通过损失规避与紧迫感驱动行动；帕金森定律指出工作会膨胀到填满 allotted 时间。",
        links=("w_cognition_attention", "w_craft_flow"),
    ),
    WorldItem(
        id="w_craft_flow", source="blog", topic="craft",
        title="心流的触发条件",
        summary="当挑战略高于技能、目标清晰且即时反馈充分时，人容易进入专注的『心流』状态。",
        links=("w_deadline_psych", "w_typography_grid"),
    ),
    # --- on-topic: music (the user's guitar thread) ---------------------------
    WorldItem(
        id="w_music_ear", source="journal", topic="music",
        title="绝对音与相对音",
        summary="绝对音多在幼年音乐训练窗口期形成；多数演奏者依赖相对音程判断，二者并非优劣之分。",
        links=("w_music_theory", "w_cognition_attention"),
    ),
    WorldItem(
        id="w_music_theory", source="wiki", topic="music",
        title="和声的功能进行",
        summary="共同调内的主—属—下属功能推进制造张力与解决，是西方和声的基本语法。",
        links=("w_music_ear", "w_typography_grid"),
    ),
    # --- edge: cognition (adjacent to memory) ---------------------------------
    WorldItem(
        id="w_cognition_attention", source="science_daily", topic="cognition",
        title="注意力的瓶颈",
        summary="工作记忆容量有限，选择性注意在竞争刺激中过滤信息，决定了哪些内容进入编码。",
        links=("w_sleep_memory", "w_deadline_psych", "w_music_ear"),
    ),
    # --- edge: nutrition (adjacent to food) -----------------------------------
    WorldItem(
        id="w_nutrition_gut", source="journal", topic="nutrition",
        title="肠道菌群与饮食",
        summary="膳食纤维喂养特定菌群产生短链脂肪酸；菌群构成受长期膳食结构缓慢塑造。",
        links=("w_umami", "w_mycology_fungi"),
    ),
    # --- alien: astrophysics (never raised by the user) -----------------------
    WorldItem(
        id="w_astro_formation", source="science_daily", topic="astrophysics",
        title="行星如何形成",
        summary="原行星盘中的尘埃逐级碰撞吸积成星子，再在引力作用下聚成行星，轨道由角动量决定。",
        links=("w_astro_entropy",),
    ),
    WorldItem(
        id="w_astro_entropy", source="journal", topic="astrophysics",
        title="熵与宇宙的演化",
        summary="孤立系统熵增给出时间的箭头；恒星燃烧低熵核燃料，最终把能量以辐射形式耗散。",
        links=("w_astro_formation", "w_mycology_fungi"),
    ),
    # --- alien: mycology (never raised by the user) ---------------------------
    WorldItem(
        id="w_mycology_fungi", source="wiki", topic="mycology",
        title="真菌的营养与网络",
        summary="真菌以菌丝分泌酶分解有机物并吸收养分，地下菌丝网络可在树木间传递信号与养分。",
        links=("w_nutrition_gut", "w_astro_entropy"),
    ),
    WorldItem(
        id="w_mycology_symbiosis", source="science_daily", topic="mycology",
        title="菌与植物的共生",
        summary="菌根真菌扩展植物根系吸收磷与水分，植物则以光合产物回馈，形成互利共生。",
        links=("w_mycology_fungi",),
    ),
    # --- alien: history (never raised by the user) ----------------------------
    WorldItem(
        id="w_history_oral", source="museum", topic="history",
        title="口述传统的可靠性",
        summary="代际口传在韵律与仪式中保存主干叙事，细节随时间漂移，需与考古证据交叉校验。",
        links=("w_autobiographical", "w_history_trade"),
    ),
    WorldItem(
        id="w_history_trade", source="museum", topic="history",
        title="古道与贸易网络",
        summary="长途贸易线路连接不同生态区，商品、技术与观念沿路线扩散并塑造沿途聚落。",
        links=("w_history_oral",),
    ),
    # --- alien: typography (never raised by the user) -------------------------
    WorldItem(
        id="w_typography_grid", source="studio_blog", topic="typography",
        title="版面网格系统",
        summary="网格用列、 gutter 与基线统一排版节奏，约束反而让页面在秩序中保持可读性。",
        links=("w_craft_flow", "w_music_theory"),
    ),
    WorldItem(
        id="w_typography_serif", source="studio_blog", topic="typography",
        title="衬线的历史",
        summary="衬线源于刻写工具的收刀痕迹，现代衬线与无衬线字体的选择更多关乎风格与场景而非阅读速度。",
        links=("w_typography_grid",),
    ),
)


class WorldCorpus:
    """A read-only view over a fixed set of :class:`WorldItem`.

    All lookups are by exact id / topic / source, so selection downstream is
    deterministic. ``links`` give the bounded graph the accident mode walks.
    """

    def __init__(self, items: tuple[WorldItem, ...] | list[WorldItem]):
        self.items: tuple[WorldItem, ...] = tuple(items)
        self._by_id: dict[str, WorldItem] = {it.id: it for it in self.items}

    def __len__(self) -> int:
        return len(self.items)

    def get(self, item_id: str) -> WorldItem | None:
        return self._by_id.get(item_id)

    def topics(self) -> list[str]:
        """Distinct topics in corpus order (deterministic)."""
        seen: list[str] = []
        for it in self.items:
            if it.topic not in seen:
                seen.append(it.topic)
        return seen

    def sources(self) -> list[str]:
        seen: list[str] = []
        for it in self.items:
            if it.source not in seen:
                seen.append(it.source)
        return seen

    def by_topic(self, topic: str) -> list[WorldItem]:
        return [it for it in self.items if it.topic == topic]

    def by_source(self, source: str) -> list[WorldItem]:
        return [it for it in self.items if it.source == source]

    def outgoing_links(self, item: WorldItem) -> list[WorldItem]:
        """The corpus items ``item`` links to (in listed order, unknown ids dropped)."""
        out: list[WorldItem] = []
        for lid in item.links:
            target = self._by_id.get(lid)
            if target is not None:
                out.append(target)
        return out


#: The default fixed world. A module-level singleton instance.
DEFAULT_WORLD_CORPUS: WorldCorpus = WorldCorpus(DEFAULT_WORLD_ITEMS)
