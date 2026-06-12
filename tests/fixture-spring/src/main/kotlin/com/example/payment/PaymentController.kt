package com.example.payment

import org.springframework.http.ResponseEntity
import org.springframework.web.bind.annotation.*

@RestController
@RequestMapping("/api/v1/payments")
class PaymentController(
    private val paymentService: PaymentService,
    private val paymentValidator: PaymentValidator,
) {
    @PostMapping("/charge")
    fun charge(@RequestBody req: ChargeRequest): ResponseEntity<ChargeResult> {
        paymentValidator.validate(req)
        return ResponseEntity.ok(paymentService.charge(req))
    }

    @PostMapping("/{id}/refund")
    fun refund(@PathVariable id: String): ResponseEntity<RefundResult> {
        return ResponseEntity.ok(paymentService.refund(id))
    }

    @GetMapping("/{id}")
    fun getPayment(@PathVariable id: String): ResponseEntity<PaymentDto> {
        return ResponseEntity.ok(paymentService.findById(id))
    }
}
