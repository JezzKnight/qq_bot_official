-- v010：视频分析笔记缓存
-- 说明：以规范化视频键（B 站 BV+分P / 抖音视频 id / 直链 URL hash）为唯一键，
-- 保存已经生成过的视频笔记，重复视频优先复用，避免重复下载、抽帧和视觉调用。

CREATE TABLE IF NOT EXISTS video_notes (
    id             BIGSERIAL PRIMARY KEY,
    video_key      TEXT NOT NULL UNIQUE,
    platform       TEXT NOT NULL,
    video_id       TEXT NOT NULL DEFAULT '',
    source_url     TEXT NOT NULL DEFAULT '',
    title          TEXT NOT NULL DEFAULT '',
    duration       DOUBLE PRECISION NOT NULL DEFAULT 0,
    note           TEXT NOT NULL,
    frame_count    INT NOT NULL DEFAULT 0,
    subtitle_chars INT NOT NULL DEFAULT 0,
    metadata       JSONB NOT NULL DEFAULT '{}'::jsonb,
    hit_count      INT NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_video_notes_updated_at
    ON video_notes (updated_at DESC);

COMMENT ON TABLE video_notes IS '视频分析笔记缓存：按规范化视频键复用已生成的笔记';
COMMENT ON COLUMN video_notes.id IS '自增主键';
COMMENT ON COLUMN video_notes.video_key IS '规范化视频键（B 站 BV+分P / 抖音视频 id / 直链 URL hash）';
COMMENT ON COLUMN video_notes.platform IS '视频平台：bilibili / douyin / direct / other';
COMMENT ON COLUMN video_notes.video_id IS '平台内视频 id（B 站 BV/av、抖音数字 id；直链为空）';
COMMENT ON COLUMN video_notes.source_url IS '首次分析时的原始 URL';
COMMENT ON COLUMN video_notes.title IS '视频标题';
COMMENT ON COLUMN video_notes.duration IS '视频时长（秒）';
COMMENT ON COLUMN video_notes.note IS '完整视频分析笔记（含标题、正文和局限）';
COMMENT ON COLUMN video_notes.frame_count IS '生成该笔记时抽取的关键帧数';
COMMENT ON COLUMN video_notes.subtitle_chars IS '生成该笔记时使用的字幕字符数';
COMMENT ON COLUMN video_notes.metadata IS '附加元信息（警告、模型等，JSONB）';
COMMENT ON COLUMN video_notes.hit_count IS '被重复视频命中的次数';
COMMENT ON COLUMN video_notes.created_at IS '首次生成时间';
COMMENT ON COLUMN video_notes.updated_at IS '最近更新时间';
COMMENT ON COLUMN video_notes.last_used_at IS '最近一次命中时间';
