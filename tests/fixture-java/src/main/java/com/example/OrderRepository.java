package com.example;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

@Repository
public class OrderRepository implements OrderStore {
    private final JdbcTemplate jdbcTemplate;

    public OrderRepository(JdbcTemplate jdbcTemplate) {
        this.jdbcTemplate = jdbcTemplate;
    }

    public Object findById(Long id) { return jdbcTemplate.queryForObject("SELECT 1", Object.class); }
    public Object save(Object o) { return jdbcTemplate.update("INSERT INTO orders VALUES (?)"); }
    public boolean existsById(Long id) { return false; }
}
