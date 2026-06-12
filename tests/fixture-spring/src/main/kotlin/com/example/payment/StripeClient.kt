package com.example.payment

import org.springframework.cloud.openfeign.FeignClient
import org.springframework.web.bind.annotation.PostMapping

@FeignClient(name = "stripe-api", url = "\${stripe.base-url}")
interface StripeClient {
    @PostMapping("/v1/charges")
    fun charge(payment: Payment): ChargeResult

    @PostMapping("/v1/refunds")
    fun refund(payment: Payment): RefundResult
}
