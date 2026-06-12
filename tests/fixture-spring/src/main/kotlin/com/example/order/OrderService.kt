package com.example.order

import org.springframework.kafka.core.KafkaTemplate
import org.springframework.stereotype.Service

@Service
class OrderService(
    private val orderRepository: OrderRepository,
    private val inventoryClient: InventoryClient,
    private val kafkaTemplate: KafkaTemplate<String, Any>,
) {
    fun placeOrder(req: PlaceOrderRequest): OrderDto {
        inventoryClient.reserve(req.sku, req.quantity)
        val order = orderRepository.save(Order(req))
        kafkaTemplate.send("orders.placed", order.id, order)
        return OrderDto(order)
    }

    fun cancelOrder(id: String) {
        val order = orderRepository.findById(id)
        orderRepository.save(order.copy(status = "CANCELLED"))
        kafkaTemplate.send("orders.cancelled", id, order)
    }

    fun findById(id: String): OrderDto = OrderDto(orderRepository.findById(id))
}
