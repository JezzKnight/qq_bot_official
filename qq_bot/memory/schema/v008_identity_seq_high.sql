-- v008：身份序列起点上移到 10^13，彻底避开 10 位 QQ 号段
-- 背景：OneBot 自映射要求 internal_id 等于 QQ 号；若序列从 10 亿起，存在与 10 位 QQ 号
-- 在 identity_map.internal_id 唯一约束上冲突的理论可能。序列上移后两类 id 永不相交。
-- 注意：ALTER SEQUENCE 必须同时调整 START，否则 PG 报 "START value cannot be less than MINVALUE"。
ALTER SEQUENCE identity_seq
    MINVALUE 10000000000000
    START WITH 10000000000000
    RESTART WITH 10000000000000;
