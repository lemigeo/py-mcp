-- 적용: docker exec -i py-mcp-mysql mysql -uroot -proot mcp < sql/schema.sql

CREATE TABLE IF NOT EXISTS category (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '의미 없는 sequence (조인/참조에 사용 금지)',
  category_id BIGINT UNSIGNED NOT NULL COMMENT '카테고리 업무 키 (조인/참조 기준)',
  parent_id   BIGINT UNSIGNED NULL COMMENT '상위 카테고리의 category_id (최상위는 NULL)',
  name        VARCHAR(100)    NOT NULL,
  en_name     VARCHAR(100)    NOT NULL,
  latitude    DECIMAL(10,7)   NULL COMMENT '위도',
  longitude   DECIMAL(10,7)   NULL COMMENT '경도',
  PRIMARY KEY (id),
  UNIQUE KEY uq_category_category_id (category_id),
  KEY idx_category_parent (parent_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='상품 카테고리';

CREATE TABLE IF NOT EXISTS product (
  id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  name         VARCHAR(200)    NOT NULL,
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='상품';

CREATE TABLE IF NOT EXISTS product_category (
  product_id   BIGINT UNSIGNED NOT NULL,
  category_id  BIGINT UNSIGNED NOT NULL,
  PRIMARY KEY (product_id, category_id),
  KEY idx_pc_category (category_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='상품-카테고리 N:M';

CREATE TABLE IF NOT EXISTS product_score (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  product_id    BIGINT UNSIGNED NOT NULL,
  score         DECIMAL(16,8)    NOT NULL DEFAULT 0 COMMENT '점수',
  PRIMARY KEY (id),
  UNIQUE KEY uq_score_product_id (product_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='상품 점수';
