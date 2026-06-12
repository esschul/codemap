package com.example.order

import org.springframework.http.ResponseEntity
import org.springframework.web.bind.annotation.*

@RestController
@RequestMapping("/api/v1/orders")
class OrderController(
    private val orderService: OrderService,
) {
    @PostMapping
    fun placeOrder(@RequestBody req: PlaceOrderRequest): ResponseEntity<OrderDto> {
        return ResponseEntity.ok(orderService.placeOrder(req))
    }

    @DeleteMapping("/{id}")
    fun cancelOrder(@PathVariable id: String): ResponseEntity<Void> {
        orderService.cancelOrder(id)
        return ResponseEntity.noContent().build()
    }

    @GetMapping("/{id}")
    fun getOrder(@PathVariable id: String): ResponseEntity<OrderDto> {
        return ResponseEntity.ok(orderService.findById(id))
    }
}
