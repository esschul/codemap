package com.example.order

import org.springframework.cloud.openfeign.FeignClient
import org.springframework.web.bind.annotation.PostMapping

@FeignClient(name = "inventory-service")
interface InventoryClient {
    @PostMapping("/reservations")
    fun reserve(sku: String, qty: Int): ReservationResult
}
