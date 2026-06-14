package com.example;

import org.springframework.stereotype.Repository;

@Repository
public class OrderRepository implements OrderStore {
    public Object findById(Long id) { return null; }
    public Object save(Object o) { return null; }
    public boolean existsById(Long id) { return false; }
}
