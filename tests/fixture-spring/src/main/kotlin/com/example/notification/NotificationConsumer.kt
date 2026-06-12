package com.example.notification

import org.springframework.kafka.annotation.KafkaListener
import org.springframework.stereotype.Service

@Service
class NotificationConsumer(
    private val emailClient: EmailClient,
    private val notificationRepository: NotificationRepository,
) {
    @KafkaListener(topics = ["orders.placed"])
    fun onOrderPlaced(event: OrderPlacedEvent) {
        emailClient.sendReceipt(event.customerId, event.orderId)
        notificationRepository.save(Notification(event))
    }
}
