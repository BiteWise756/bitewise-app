from pydantic import BaseModel, EmailStr, Field
from typing import Optional, List, Dict, Any
from datetime import datetime

class AuthRequest(BaseModel):
    email: str
    password: str
    displayName: Optional[str] = None
    accountType: Optional[str] = None

class ResendVerifyRequest(BaseModel):
    email: str

class FeedbackRequest(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    message: str

class RatingRequest(BaseModel):
    rating: int
    comment: Optional[str] = None

class SupportReplyRequest(BaseModel):
    reply: str

class SupportApprovalRequest(BaseModel):
    approve: bool
    edited_reply: Optional[str] = None

class SupportAiToggleRequest(BaseModel):
    enabled: bool

class VerifyRequest(BaseModel):
    email: str
    code: str

class ForgotPasswordRequest(BaseModel):
    email: str

class ResetPasswordRequest(BaseModel):
    email: str
    code: str
    newPassword: str

class GoogleAuthRequest(BaseModel):
    idToken: str

class FacebookAuthRequest(BaseModel):
    idToken: str

class AuthResponse(BaseModel):
    uid: str
    email: str
    token: str
    displayName: Optional[str] = None

class ProfileUpdate(BaseModel):
    displayName: Optional[str] = None
    age: Optional[int] = None
    weight: Optional[float] = None
    height: Optional[float] = None
    goal: Optional[str] = None
    dietaryPreferences: Optional[List[str]] = None
    allergies: Optional[List[str]] = None
    onboardingCompleted: Optional[bool] = None

class Detection(BaseModel):
    className: str
    confidence: float
    isFood: bool
    nutrition: Dict[str, Any] = {}

class MealAnalysis(BaseModel):
    foodName: str
    calories: Any
    protein: Any
    fat: Any
    carbs: Any
    fiber: Optional[Any] = 0
    sugar: Optional[Any] = 0
    sodium: Optional[Any] = 0
    healthScore: int
    status: str = "success"
    message: Optional[str] = None
    detections: List[Detection] = []
    totalDetected: Optional[int] = 0
    totalFoodItems: Optional[int] = 0
    healthyTips: List[str] = []
    betterAlternatives: List[str] = []

class MealHistory(BaseModel):
    userId: str
    foodName: str
    calories: float
    protein: float
    fat: float
    carbs: float
    healthScore: int
    timestamp: datetime
    imageUrl: Optional[str] = None

class MenuAnalysis(BaseModel):
    restaurantName: Optional[str] = None
    items: List[Dict[str, Any]] = []
    healthiestOptions: List[str] = []
    suggestions: List[str] = []

class PantryAnalysis(BaseModel):
    ingredients: List[str] = []
    recipes: List[Dict[str, Any]] = []

class BasketAnalysis(BaseModel):
    healthyItems: List[str] = []
    unhealthyItems: List[str] = []
    healthScore: int
    suggestions: List[str] = []

class ProductAnalysis(BaseModel):
    productName: str
    ingredients: List[str] = []
    nutritionFacts: Dict[str, float] = {}
    warnings: List[str] = []
    healthScore: int

class NutritionPlan(BaseModel):
    userId: str
    date: str
    meals: Dict[str, Any]
    totalCalories: float
    waterGoal: int
    tips: List[str] = []

class ChatMessage(BaseModel):
    message: str
    userId: Optional[str] = None

class ChatResponse(BaseModel):
    reply: str
    timestamp: datetime

class WaterLog(BaseModel):
    userId: str
    amount: int
    timestamp: datetime

class WeightLog(BaseModel):
    userId: str
    weight: float
    date: str

class Report(BaseModel):
    userId: str
    period: str
    data: Dict[str, Any]

class FamilyMember(BaseModel):
    userId: str
    name: str
    relationship: str
    age: Optional[int] = None
    weight: Optional[float] = None
    goal: Optional[str] = None

class HealthSwap(BaseModel):
    originalFood: str
    alternativeFood: str
    caloriesSaved: int
    benefits: List[str] = []

class BudgetMeal(BaseModel):
    budget: float
    mealPlan: Dict[str, Any]
    estimatedCost: float
    groceryList: List[str] = []

class MoodMeal(BaseModel):
    mood: str
    recommendations: List[Dict[str, Any]] = []

class Achievement(BaseModel):
    name: str
    description: str
    icon: str
    unlocked: bool = False
    unlockedAt: Optional[datetime] = None

class DoctorNoteRequest(BaseModel):
    userId: str
    doctorId: str = "doctor"
    note: str
    diagnosis: Optional[str] = None
    prescription: Optional[str] = None

class DoctorNote(BaseModel):
    id: str
    userId: str
    doctorId: str
    note: str
    diagnosis: Optional[str] = None
    prescription: Optional[str] = None
    createdAt: datetime

class AIHistoryEntry(BaseModel):
    role: str
    content: str
    timestamp: datetime

class AIHistorySession(BaseModel):
    sessionId: str
    messages: List[AIHistoryEntry]
    createdAt: datetime

class UserProgressSummary(BaseModel):
    profile: Dict[str, Any]
    recentMeals: List[Dict[str, Any]]
    nutritionStats: Dict[str, Any]
    weightHistory: List[Dict[str, Any]]
    waterIntake: Dict[str, Any]
    aiHistory: List[Dict[str, Any]]
    doctorNotes: List[Dict[str, Any]]


# ---- Payment / Subscription models ----
class Plan(BaseModel):
    id: str
    name: str
    price_cents: int
    currency: str = "USD"
    billing_period: str  # "monthly" or "yearly"
    description: Optional[str] = None
    features: List[str] = []


class PaymentCreateRequest(BaseModel):
    plan_id: str


class PaymentCreateResponse(BaseModel):
    payment_id: str
    payment_url: Optional[str] = None
    status: str = "PENDING"
    plan: Plan
    expires_at: datetime


class PaymentStatusResponse(BaseModel):
    payment_id: str
    status: str  # PENDING, PAID, FAILED, CANCELLED, EXPIRED, REFUNDED
    plan_id: str
    amount_cents: int
    currency: str
    created_at: datetime
    paid_at: Optional[datetime] = None
    provider_transaction_id: Optional[str] = None
    payment_url: Optional[str] = None  # Paymob checkout URL for redirect


class WebhookPayload(BaseModel):
    # Generic webhook payload - provider-specific fields handled in service
    payment_id: str
    provider: str
    status: str
    amount_cents: Optional[int] = None
    currency: Optional[str] = None
    provider_transaction_id: Optional[str] = None
    raw_payload: Optional[Dict[str, Any]] = None


class SubscriptionResponse(BaseModel):
    user_id: str
    plan_id: str
    status: str  # ACTIVE, INACTIVE, CANCELLED, PAST_DUE
    current_period_start: Optional[datetime] = None
    current_period_end: Optional[datetime] = None
    payment_id: Optional[str] = None
    provider_subscription_id: Optional[str] = None