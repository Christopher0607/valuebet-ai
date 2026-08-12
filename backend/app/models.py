"""
Database models — SQLite via SQLAlchemy.
Mirrors the structure we designed for Supabase, but self-contained
in a single file on disk (valuebet.db), no external service needed.
"""
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, Boolean, Date, DateTime, ForeignKey,
    UniqueConstraint, event,
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from datetime import datetime
import os

# 本地跑用 SQLite（一键启动，零配置）；部署到云上时用 DATABASE_URL 指向
# Supabase 的 Postgres。两种数据库的连接参数完全不同，所以下面按 scheme 分叉。
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./valuebet.db")

# Supabase 控制台给的连接串是 "postgres://"，SQLAlchemy 2.x 只认
# "postgresql://"。不转换的话启动时会报 "Can't load plugin: sqlalchemy.dialects:postgres"，
# 而这个报错跟真正的原因（少了三个字母）看起来毫无关系，很难查。
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

IS_SQLITE = DATABASE_URL.startswith("sqlite")

if IS_SQLITE:
    # timeout=30：SQLAlchemy 默认只等 5 秒就抛 "database is locked"。
    # 启动时调度器会在后台线程立刻跑一次全量更新（1700+ 场比赛和预测，
    # 几十秒的密集写入），这期间前端的 API 读请求会撞上写锁。5 秒根本不够，
    # 于是首页加载就报 500，界面显示「无法连接本地后端」——用户以为服务没起，
    # 其实是起来了正在写库。Windows 上磁盘慢加上 Defender 扫描写入，
    # 锁窗口比 Linux 宽得多，所以这个 bug 在开发机上几乎复现不到。
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        """每条新连接都设一次 PRAGMA —— 它们是连接级的，不是数据库级的。

        journal_mode=WAL 是这里的关键：默认的 delete 模式下，**一个写事务会阻塞
        全部读**；WAL 模式下读写可以并发，长时间的更新不再让整个界面瘫掉。
        WAL 是持久属性（写进数据库文件头），但重复设置无害。

        busy_timeout 是 connect_args 里 timeout 的兜底：某些路径下
        （比如 SQLAlchemy 内部新建的连接）那个参数不一定传得到。
        """
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA synchronous=NORMAL")   # WAL 下这一档足够安全，写入快很多
        cur.close()
else:
    # Postgres。pool_pre_ping 是必须的：Supabase 会掐掉闲置连接，
    # 不 ping 的话池子里的死连接要等到下一次查询才暴露，表现为随机 500。
    # pool_recycle 比 Supabase 的闲置超时短，主动换掉长连接。
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,
        pool_recycle=280,
        pool_size=5,
        max_overflow=5,
    )
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Competition(Base):
    __tablename__ = "competitions"
    id = Column(Integer, primary_key=True)
    code = Column(String, unique=True, nullable=False)          # 'wc2026' | 'ucl2627'
    name = Column(String, nullable=False)
    name_zh = Column(String)
    data_source = Column(String)                                  # results feed URL
    odds_sport_key = Column(String)                                # unused locally, kept for parity
    is_active = Column(Boolean, default=True)


class Match(Base):
    __tablename__ = "matches"
    id = Column(Integer, primary_key=True)
    competition_id = Column(Integer, ForeignKey("competitions.id"))
    date = Column(Date, nullable=False)
    time_utc = Column(String)
    team1 = Column(String, nullable=False)
    team2 = Column(String, nullable=False)
    score1 = Column(Integer, nullable=True)
    score2 = Column(Integer, nullable=True)
    round = Column(String)
    grp = Column(String)
    ground = Column(String)
    status = Column(String, default="upcoming")                    # 'upcoming' | 'played'
    bayesian_folded_in = Column(Boolean, default=False)
    # Tracks whether this match's result has already been folded into its
    # two teams' Bayesian posteriors. Without this, update_bayesian_states_
    # for_newly_played_matches() re-applies every played match's score on
    # every run -- measured directly: three consecutive manual "update now"
    # calls with zero new matches still moved Mexico's attack estimate from
    # 1.1606 to 1.2124 to 1.2522, purely from re-processing the same results.
    # This column is the actual fix, not just a documented caveat.
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    competition = relationship("Competition")


class Prediction(Base):
    __tablename__ = "predictions"
    id = Column(Integer, primary_key=True)
    match_id = Column(Integer, ForeignKey("matches.id"), unique=True)
    model = Column(String, default="dixon-coles")
    prob_home = Column(Float)
    prob_draw = Column(Float)
    prob_away = Column(Float)
    xg_home = Column(Float)
    xg_away = Column(Float)
    attack_home = Column(Float)
    defense_home = Column(Float)
    attack_away = Column(Float)
    defense_away = Column(Float)
    predicted = Column(String)                                      # 'win1' | 'draw' | 'win2'
    is_correct = Column(Boolean, nullable=True)
    rps = Column(Float, nullable=True)

    match = relationship("Match")


class Odds(Base):
    """手输赔率的记录，用来预填「预测」页的赔率表单和算 EV。

    加 owner_id 是因为这份数据本质上是"你自己看到的报价"——BK8 之类
    平台不同账号、不同时间点报价可能不一样，而且它会原样预填进表单，
    没有隔离的话账号 A 填过的赔率会在账号 B 打开同一场比赛时冒出来，
    虽然不是钱，但也是账号之间不该互相看到的个人输入内容。
    跟 Bet/RealBet/ParlayBet/Withdrawal 用同一套隔离机制。
    """
    __tablename__ = "odds"
    id = Column(Integer, primary_key=True)
    match_id = Column(Integer, ForeignKey("matches.id"))
    owner_id = Column(String, nullable=True, index=True)
    source = Column(String, default="manual")
    odds_home = Column(Float)
    odds_draw = Column(Float, nullable=True)
    odds_away = Column(Float)
    recorded_at = Column(DateTime, default=datetime.utcnow)


class MarketOdds(Base):
    """The Odds API 自动抓回来的**市场公开报价**，一场比赛一行，每次抓覆盖。

    为什么不塞进 Odds 表：那张表是"你自己看到/填的报价"，带 owner_id、按
    账号隔离，而且是 EV 计算和下注记录的依据。市场报价是公开数据，没有
    归属，混进去会有两个具体的坏处：
      1. _owned() 在云端模式下会把 owner_id 为空的行判成不可见，自动抓
         回来的赔率反而永远显示不出来；
      2. 「系统抓的价」和「我在 BK8 实际能拿到的价」是两回事，混在一条
         时间线里，下注记录里的 odds_used 就说不清到底是哪个。
    所以分开存，前端也分开展示：市场价只用来预填和比价，真正下注用的
    还是用户自己确认的那个价。

    同时存最优价和平均价：项目走查出来的唯一正结果（热门-冷门偏差）盈亏
    完全取决于价格执行——成交价 = 平均价 + f×(最优价-平均价)，f=0 时
    ROI -3.54%，f=0.8 才盈亏平衡。只留一个价，这个差额就没法衡量了。
    """
    __tablename__ = "market_odds"
    id = Column(Integer, primary_key=True)
    match_id = Column(Integer, ForeignKey("matches.id"), unique=True)
    n_books = Column(Integer)                                       # 参与比价的博彩公司家数
    best_home = Column(Float)
    best_draw = Column(Float, nullable=True)
    best_away = Column(Float)
    avg_home = Column(Float)
    avg_draw = Column(Float, nullable=True)
    avg_away = Column(Float)
    fetched_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AppState(Base):
    """一张极简的键值表，存"整个实例共享、跟用户无关"的小状态。

    目前只有一个 key：params_fingerprint —— 参数表文件的指纹。
    update_predictions 靠它判断"参数表有没有重新训练过"：没变就只算新比赛，
    变了就全量重算。存在库里而不是内存里，是因为 Render 免费档一休眠就
    重启进程，内存里的指纹每次都是空的，那样每次冷启动都会全量重算，
    等于没优化。
    """
    __tablename__ = "app_state"
    key = Column(String, primary_key=True)
    value = Column(String)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Bet(Base):
    """Virtual bets — for mathematically testing the model, not real money."""
    __tablename__ = "bets"
    id = Column(Integer, primary_key=True)
    match_id = Column(Integer, ForeignKey("matches.id"))
    # 每个登录账号只能看到、操作自己的下注——没有这一列的话所有账号
    # 共用同一份注单和同一条资金曲线，换个账号登录看到的是别人的实盘
    # 记录和余额。本地不登录时固定用 "local"，见 main.py 里的 _owner_key()。
    owner_id = Column(String, nullable=True, index=True)
    outcome = Column(String, nullable=False)                        # 'home' | 'draw' | 'away'
    stake = Column(Float, default=100)
    odds_used = Column(Float)
    ev_at_bet = Column(Float)
    kelly_pct = Column(Float)
    model_prob = Column(Float)
    result = Column(String, default="pending")                      # 'win' | 'loss' | 'pending'
    pnl = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    match = relationship("Match")


class RealBet(Base):
    """Real-money bets, entered manually after you place them on BK8/etc.
    This app never places bets automatically — see the top-level README
    for why that's intentionally not something this system does."""
    __tablename__ = "real_bets"
    id = Column(Integer, primary_key=True)
    match_id = Column(Integer, ForeignKey("matches.id"))
    competition_id = Column(Integer, ForeignKey("competitions.id"))
    owner_id = Column(String, nullable=True, index=True)          # 同 Bet.owner_id
    platform = Column(String, default="bk8")
    outcome = Column(String, nullable=False)
    stake_real = Column(Float, nullable=False)
    currency = Column(String, default="HKD")
    odds_used = Column(Float, nullable=False)
    model_prob_at_bet = Column(Float)
    ev_at_bet = Column(Float)
    kelly_suggested_pct = Column(Float)
    kelly_suggested_amount = Column(Float)
    actually_followed_kelly = Column(Boolean, nullable=True)
    result = Column(String, default="pending")
    payout_real = Column(Float, nullable=True)
    pnl_real = Column(Float, nullable=True)
    placed_at = Column(DateTime, default=datetime.utcnow)
    settled_at = Column(DateTime, nullable=True)

    match = relationship("Match")


class ParlayBet(Base):
    """
    串关注单（虚拟盘和实盘共用一张表，用 kind 区分）。

    为什么不像 Bet/RealBet 那样拆成两张表：串关的结算逻辑（所有腿全中才算赢、
    任何一腿输了立刻判负）比单场复杂得多，拆两张表意味着这套逻辑要写两遍、
    以后改也要改两处。用一个 kind 字段区分，结算逻辑只写一次。

    为什么单场注单不能装串关：Bet.match_id 是单场比赛的外键，一行对应一场。
    串关是 3-8 场，现有表结构表达不了——这才是之前串关页没有「记录注单」
    按钮的真实原因，不是漏做了 UI，是数据模型根本存不下。
    """
    __tablename__ = "parlay_bets"
    id = Column(Integer, primary_key=True)
    owner_id = Column(String, nullable=True, index=True)          # 同 Bet.owner_id
    kind = Column(String, nullable=False, default="virtual")        # 'virtual' | 'real'
    stake = Column(Float, nullable=False)
    odds_used = Column(Float, nullable=False)
    # 实际串关总赔率。通常约等于各腿赔率相乘，但博彩公司可能有自己的串关定价，
    # 所以允许用户在记录实盘时填入真实拿到的总赔率，而不是强制用乘积。
    joint_probability = Column(Float)
    ev_at_bet = Column(Float)
    kelly_pct = Column(Float)
    platform = Column(String, default="bk8")                         # 仅 kind='real' 时有意义
    currency = Column(String, default="HKD")
    result = Column(String, default="pending")                       # 'pending' | 'win' | 'loss'
    pnl = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    settled_at = Column(DateTime, nullable=True)

    legs = relationship("ParlayLeg", back_populates="parlay", cascade="all, delete-orphan")


class ParlayLeg(Base):
    """串关的单条腿。一注串关有 2-8 条。"""
    __tablename__ = "parlay_legs"
    id = Column(Integer, primary_key=True)
    parlay_bet_id = Column(Integer, ForeignKey("parlay_bets.id", ondelete="CASCADE"), nullable=False)
    match_id = Column(Integer, ForeignKey("matches.id"), nullable=False)
    outcome = Column(String, nullable=False)                         # 'home' | 'draw' | 'away'
    leg_odds = Column(Float)
    leg_prob = Column(Float)

    parlay = relationship("ParlayBet", back_populates="legs")
    match = relationship("Match")


class UserSettings(Base):
    """
    每个账号一份（资金总额、凯利比例这些）。

    没有单独加 owner_id 列——直接复用已经存在的 setting_key（本来就有
    unique 约束，语义正好是"一个键对应一份设置"）。云端用 Supabase 的
    user id 当 setting_key，本地固定用 "default"，跟以前的行为完全一样，
    不需要迁移旧数据、也不用给这张表打 schema 补丁。
    """
    __tablename__ = "user_settings"
    id = Column(Integer, primary_key=True)
    setting_key = Column(String, unique=True, default="default")
    bankroll_total = Column(Float, default=10000)
    kelly_fraction = Column(Float, default=0.5)
    max_bet_pct = Column(Float, default=0.15)
    min_ev_threshold = Column(Float, default=0.03)


class Withdrawal(Base):
    """
    实盘资金的提款记录——赢了钱从 BK8 等平台转去自己银行账户后，
    在这里登记一笔，让「实盘」这条资金曲线跟真实情况对得上。

    只作用于实盘，没有虚拟盘版本：虚拟盘是纯粹用来测试模型的假钱，
    不存在"从假账户提现"这回事。

    金额恒为正数（代表提出多少），跟 bankroll_summary 里结算事件的处理
    方式一样：提款记一个带日期的事件，只从发生的那天起影响资金曲线——
    不能直接去改 UserSettings.bankroll_total，那样会把提款之前的历史
    也一起下移，等于篡改了过去已经发生的事。
    """
    __tablename__ = "withdrawals"
    id = Column(Integer, primary_key=True)
    owner_id = Column(String, nullable=True, index=True)          # 同 Bet.owner_id
    amount = Column(Float, nullable=False)
    currency = Column(String, default="HKD")
    note = Column(String, nullable=True)
    withdrawn_at = Column(DateTime, default=datetime.utcnow)


class BayesianTeamStateRow(Base):
    """
    Persists BayesianTeamState (see model.py) across backend restarts.
    Without this table, every posterior update would live only in memory
    and vanish the moment uvicorn restarts -- which would make "real-time
    Bayesian updating" a lie in practice, since the whole point is that a
    team's parameters keep drifting with recent form across many matches,
    not just within a single process lifetime.

    One row per (team_name, competition_id) -- a team's Bayesian state is
    scoped to a competition, since e.g. Spain's national-team form and
    a Spanish club's league form are unrelated quantities.
    """
    __tablename__ = "bayesian_team_states"
    id = Column(Integer, primary_key=True)
    team_name = Column(String, nullable=False)
    competition_id = Column(Integer, ForeignKey("competitions.id"), nullable=False)
    attack_shape = Column(Float, nullable=False)
    attack_rate = Column(Float, nullable=False)
    defense_theta_shape = Column(Float, nullable=False)
    defense_theta_rate = Column(Float, nullable=False)
    decay = Column(Float, default=0.98)
    n_updates = Column(Integer, default=0)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("team_name", "competition_id", name="uq_team_competition"),
    )


class UpdateLog(Base):
    """Every scheduler run writes a row here, so the frontend can show
    'last updated' / 'next update' honestly instead of just claiming it."""
    __tablename__ = "update_log"
    id = Column(Integer, primary_key=True)
    ran_at = Column(DateTime, default=datetime.utcnow)
    matches_updated = Column(Integer, default=0)
    predictions_updated = Column(Integer, default=0)
    bets_resolved = Column(Integer, default=0)
    status = Column(String, default="ok")                           # 'ok' | 'error'
    detail = Column(String, nullable=True)


class PriceLog(Base):
    """价格捕获率的观测记录。

    为什么需要这张表：handoff/09 确认的赚钱结构是「押热门 + 拿到好价」，
    而能不能赚钱几乎完全由**你自己平台的价格有多好**决定——
    捕获率 f=0 时 ROI -1.43%，f=1 时 +1.67%，同一个策略两个方向。

    f 无法从公开数据推断，因为它取决于你用哪个平台。所以只能实测：
    每次下注前把三个价记下来，累积几十条之后 f 的均值就稳定了。
    这张表存的就是这些观测。

    f = (你的赔率 - 市场平均) / (市场最高 - 市场平均)
    """
    __tablename__ = "price_logs"
    id = Column(Integer, primary_key=True)
    logged_at = Column(DateTime, default=datetime.utcnow)
    match_desc = Column(String, nullable=True)        # 自由文本，方便回头核对
    platform = Column(String, default="bk8")
    selection = Column(String, nullable=True)         # 'home' | 'draw' | 'away'
    my_odds = Column(Float, nullable=False)
    market_avg = Column(Float, nullable=False)
    market_best = Column(Float, nullable=False)
    capture = Column(Float, nullable=True)            # 算出来的 f，存下来免得每次重算
    note = Column(String, nullable=True)


def init_db():
    Base.metadata.create_all(bind=engine)
    _migrate_add_missing_columns()
    _migrate_add_missing_indexes()


# create_all() 只会给"整张表都不存在"的情况建表——对已经存在的表，就算
# 模型里新加了 Column，它也完全不会去 ALTER TABLE。本地 SQLite 因为
# 每次都是从头建库（或者一键启动时新建的空库）所以天然带着最新字段，
# 从来看不出这个问题；Railway 上那个 Postgres 库是部署当天就建好的，
# 之后这个项目里至少新加过两批字段——Match.time_utc、以及各下注表的
# owner_id——每一次都被 create_all() 无声跳过，实际效果是那次改动在
# 本地测得再干净，一部署到云端就变成每次写库都报
# "column ... of relation ... does not exist"，而这类报错只在生产的
# Postgres 上才会出现，本地完全复现不出来，找起来最费劲。
#
# 用 ALTER TABLE ... ADD COLUMN IF NOT EXISTS 显式打补丁，幂等、每次
# 启动都能跑，不管列已经在不在都不会报错。新增字段以后都要在这里补一行，
# 不能只加进模型就当完事——这正是这次真实漏掉的那一步。
_SCHEMA_PATCHES = [
    ("matches", "time_utc", "VARCHAR"),
    ("bets", "owner_id", "VARCHAR"),
    ("real_bets", "owner_id", "VARCHAR"),
    ("parlay_bets", "owner_id", "VARCHAR"),
    ("withdrawals", "owner_id", "VARCHAR"),
    ("odds", "owner_id", "VARCHAR"),
]


# 外键**不会**自动带索引 —— Postgres 和 SQLite 都不会（自动建索引的只有
# 主键和 unique 约束）。实测 EXPLAIN QUERY PLAN，这三个查询全是全表扫：
#
#   SELECT * FROM matches WHERE competition_id=?   → SCAN matches
#   SELECT * FROM odds WHERE match_id IN (...)     → SCAN odds
#   SELECT * FROM bets WHERE match_id=?            → SCAN bets
#
# 对比 predictions.match_id 因为声明了 unique=True，自动有索引，同样的查询
# 是 SEARCH ... USING INDEX。
#
# 以前无所谓：matches 才一千多行。接入 6 个联赛之后是 5,964 行，而
# upsert_matches 每个赛事都要 `filter_by(competition_id=...)` 查一次——
# 一轮更新 14 个赛事就是 14 次全表扫。本地 SQLite 是进程内的，扫也就扫了；
# 云端远端 Postgres 上这是实打实的开销。
#
# CREATE INDEX IF NOT EXISTS 两边语法一致（不像 ADD COLUMN 那样要分叉），
# 幂等，每次启动都能跑。
_INDEX_PATCHES = [
    ("ix_matches_competition_id", "matches", "competition_id"),
    ("ix_matches_status_date", "matches", "status, date"),
    ("ix_odds_match_id", "odds", "match_id"),
    ("ix_bets_match_id", "bets", "match_id"),
    ("ix_real_bets_match_id", "real_bets", "match_id"),
    ("ix_parlay_legs_match_id", "parlay_legs", "match_id"),
    ("ix_parlay_legs_parlay_bet_id", "parlay_legs", "parlay_bet_id"),
]


def _migrate_add_missing_indexes():
    """建索引。跟加列分开写是因为这两件事的失败后果不一样：

    少一列会让写库直接报错，少一个索引只是慢——所以这里对单个索引的失败
    只记日志不抛，不能让一个建不出来的索引挡住整个服务启动。
    """
    import logging
    from sqlalchemy import text
    with engine.begin() as conn:
        for name, table, cols in _INDEX_PATCHES:
            try:
                conn.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({cols})"))
            except Exception as e:
                logging.getLogger("valuebet.models").warning(
                    "建索引 %s 失败（只影响速度，不影响正确性）: %s", name, str(e)[:200])


def _migrate_add_missing_columns():
    """两边语法不一样，不能一条 SQL 走天下——实测验证过，不是猜的：

    `ALTER TABLE ... ADD COLUMN IF NOT EXISTS ...` 在 Postgres 上没问题
    （9.6 起就支持），但 SQLite **从来没有**支持过这个写法，哪怕是很新的
    版本（本机 3.45.1 照样报 "near EXISTS: syntax error"）——一开始以为
    是版本问题，实际测了才发现 SQLite 压根没有这条语法，只支持不带
    IF NOT EXISTS 的裸 ADD COLUMN。

    所以 SQLite 这边改成先用 PRAGMA table_info 查这一列在不在，不在才
    执行裸 ADD COLUMN；Postgres 继续用 IF NOT EXISTS，本来就是安全的。
    """
    from sqlalchemy import text
    with engine.begin() as conn:
        for table, column, ddl_type in _SCHEMA_PATCHES:
            if IS_SQLITE:
                existing = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
                if column not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))
            else:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl_type}"))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
