package com.example.payment

import org.springframework.data.jpa.repository.JpaRepository

interface PaymentRepository : JpaRepository<Payment, String> {
    fun findByOrderId(orderId: String): Payment?
}
