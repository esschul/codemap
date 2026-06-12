package com.example.notification

import org.springframework.cloud.openfeign.FeignClient
import org.springframework.web.bind.annotation.PostMapping

@FeignClient(name = "sendgrid")
interface EmailClient {
    @PostMapping("/v3/mail/send")
    fun sendReceipt(customerId: String, orderId: String)
}
