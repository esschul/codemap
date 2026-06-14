package com.example;

public interface OrderStore {
    Object findById(Long id);
    Object save(Object o);
}
