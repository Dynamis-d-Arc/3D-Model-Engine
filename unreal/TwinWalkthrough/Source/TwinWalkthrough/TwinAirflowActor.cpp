#include "TwinAirflowActor.h"

#include "Components/InstancedStaticMeshComponent.h"
#include "Dom/JsonObject.h"
#include "Engine/StaticMesh.h"
#include "Materials/Material.h"
#include "TwinViewerDirector.h"

namespace
{
	constexpr float DotScale = 0.06f;  // the engine sphere is 100 cm across -> 6 cm dots
	constexpr float BaseSpeed = 75.f;  // cm/s at full fan speed

	bool V3(const TSharedPtr<FJsonObject>& O, const TCHAR* Key, FVector& Out)
	{
		const TArray<TSharedPtr<FJsonValue>>* A = nullptr;
		if (!O.IsValid() || !O->TryGetArrayField(Key, A) || A->Num() < 3)
		{
			return false;
		}
		Out = FVector((*A)[0]->AsNumber(), (*A)[1]->AsNumber(), (*A)[2]->AsNumber());
		return true;
	}

	FVector CatmullRom(const TArray<FVector>& P, float U)
	{
		const int32 Segs = P.Num() - 1;
		const float T = FMath::Clamp(U, 0.f, 0.9999f) * Segs;
		const int32 I = FMath::FloorToInt(T);
		const float F = T - I;
		const FVector& P0 = P[FMath::Max(I - 1, 0)];
		const FVector& P1 = P[I];
		const FVector& P2 = P[FMath::Min(I + 1, Segs)];
		const FVector& P3 = P[FMath::Min(I + 2, Segs)];
		const float F2 = F * F, F3 = F2 * F;
		return 0.5f * ((2.f * P1) + (-P0 + P2) * F + (2.f * P0 - 5.f * P1 + 4.f * P2 - P3) * F2 + (-P0 + 3.f * P1 - 3.f * P2 + P3) * F3);
	}
}

ATwinAirflowActor::ATwinAirflowActor()
{
	PrimaryActorTick.bCanEverTick = true;
	Dots = CreateDefaultSubobject<UInstancedStaticMeshComponent>(TEXT("Dots"));
	RootComponent = Dots;
	Dots->SetMobility(EComponentMobility::Movable);
	Dots->SetCollisionEnabled(ECollisionEnabled::NoCollision);
	Dots->SetCanEverAffectNavigation(false);
	Dots->SetCastShadow(false);
	Dots->NumCustomDataFloats = 3;  // RGB read by M_TwinFlow (PerInstanceCustomData 0..2)
	Dots->SetVisibility(false);
}

void ATwinAirflowActor::BeginPlay()
{
	Super::BeginPlay();
	// loaded by path (cooked via DirectoriesToAlwaysCook) so the editor works before the viewer content exists
	if (UStaticMesh* Sphere = LoadObject<UStaticMesh>(nullptr, TEXT("/Engine/BasicShapes/Sphere.Sphere")))
	{
		Dots->SetStaticMesh(Sphere);
	}
	if (UMaterialInterface* Flow = LoadObject<UMaterialInterface>(nullptr, TEXT("/Game/Viewer/Materials/M_TwinFlow.M_TwinFlow")))
	{
		Dots->SetMaterial(0, Flow);
	}
}

FLinearColor ATwinAirflowActor::TemperatureColor(float C)
{
	struct FStop { float T; FLinearColor Col; };
	static const FStop Stops[] = {
		{12.f, FLinearColor(0.05f, 0.25f, 1.0f)}, {17.f, FLinearColor(0.0f, 0.75f, 1.0f)}, {22.f, FLinearColor(0.75f, 0.95f, 1.0f)},
		{26.f, FLinearColor(1.0f, 0.65f, 0.15f)}, {31.f, FLinearColor(1.0f, 0.15f, 0.05f)}};
	if (FMath::IsNaN(C))
	{
		return FLinearColor(0.75f, 0.75f, 0.78f);  // no temperature data: neutral grey
	}
	if (C <= Stops[0].T)
	{
		return Stops[0].Col;
	}
	for (int32 i = 1; i < UE_ARRAY_COUNT(Stops); ++i)
	{
		if (C <= Stops[i].T)
		{
			return FLinearColor::LerpUsingHSV(Stops[i - 1].Col, Stops[i].Col, (C - Stops[i - 1].T) / (Stops[i].T - Stops[i - 1].T));
		}
	}
	return Stops[UE_ARRAY_COUNT(Stops) - 1].Col;
}

void ATwinAirflowActor::Clear()
{
	Paths.Reset();
	Xf.Reset();
	Units = Visible = 0;
	Dots->ClearInstances();
	Dots->SetVisibility(false);
	bShown = false;
}

void ATwinAirflowActor::SetShown(bool bInShown)
{
	bShown = bInShown && Xf.Num() > 0;
	Dots->SetVisibility(bShown);
}

void ATwinAirflowActor::Build(const TSharedPtr<FJsonObject>& Airflow, ATwinViewerDirector* InDirector)
{
	Clear();
	Director = InDirector;
	if (!Airflow.IsValid())
	{
		return;
	}
	for (const TSharedPtr<FJsonValue>& ZV : Airflow->GetArrayField(TEXT("zones")))
	{
		const TSharedPtr<FJsonObject> Z = ZV->AsObject();
		const FString UnitId = Z->GetStringField(TEXT("unit_id"));
		const float Ceiling = (float)Z->GetNumberField(TEXT("ceiling_cm"));
		FBox2D Box(ForceInit);
		const TArray<TSharedPtr<FJsonValue>>& BA = Z->GetArrayField(TEXT("box_cm"));
		if (BA.Num() == 2)
		{
			Box += FVector2D(BA[0]->AsArray()[0]->AsNumber(), BA[0]->AsArray()[1]->AsNumber());
			Box += FVector2D(BA[1]->AsArray()[0]->AsNumber(), BA[1]->AsArray()[1]->AsNumber());
		}
		auto Clamp = [&](FVector P)
		{
			if (Box.bIsValid)
			{
				P.X = FMath::Clamp(P.X, Box.Min.X, Box.Max.X);
				P.Y = FMath::Clamp(P.Y, Box.Min.Y, Box.Max.Y);
			}
			P.Z = FMath::Clamp(P.Z, 30.f, Ceiling - 4.f);
			return P;
		};
		TArray<FVector> Returns;
		for (const TSharedPtr<FJsonValue>& R : Z->GetArrayField(TEXT("return")))
		{
			FVector L;
			if (V3(R->AsObject(), TEXT("location_cm"), L))
			{
				Returns.Add(L);
			}
		}
		FVector UnitPos;
		if (Returns.Num() == 0 && V3(Z, TEXT("unit_cm"), UnitPos))
		{
			Returns.Add(FVector(UnitPos.X, UnitPos.Y, FMath::Min((float)UnitPos.Z, Ceiling)));
		}

		// sources: supply terminals with their throw directions, else the unit's own outlet
		struct FSource { FVector O; TArray<FVector> Dirs; };
		TArray<FSource> Sources;
		for (const TSharedPtr<FJsonValue>& SV : Z->GetArrayField(TEXT("supply")))
		{
			const TSharedPtr<FJsonObject> S = SV->AsObject();
			FSource Src;
			if (!V3(S, TEXT("location_cm"), Src.O))
			{
				continue;
			}
			const FString Throw = S->GetStringField(TEXT("throw"));
			const float Yaw = (float)S->GetNumberField(TEXT("rotation_yaw_deg"));
			const int32 N = Throw == TEXT("4way") ? 4 : Throw == TEXT("2way") ? 2 : (Throw == TEXT("1way") || Throw == TEXT("down")) ? 1 : 0;
			for (int32 k = 0; k < N; ++k)
			{
				Src.Dirs.Add(FRotator(0.f, Yaw + k * (360.f / FMath::Max(N, 1)), 0.f).Vector());
			}
			if (Throw == TEXT("down"))
			{
				Src.Dirs[0] = FVector::ZeroVector;
			}
			Sources.Add(Src);
		}
		if (Sources.Num() == 0)
		{
			FSource Src;
			const TArray<TSharedPtr<FJsonValue>>* D = nullptr;
			if (V3(Z, TEXT("outlet_cm"), Src.O))
			{
				Src.Dirs.Add(Z->TryGetArrayField(TEXT("outlet_dir"), D) && D->Num() >= 2
					             ? FVector((*D)[0]->AsNumber(), (*D)[1]->AsNumber(), 0.0).GetSafeNormal()
					             : FVector::ZeroVector);
				Sources.Add(Src);
			}
		}
		if (Sources.Num() == 0)
		{
			continue;
		}
		++Units;
		for (const FSource& Src : Sources)
		{
			const FVector O(Src.O.X, Src.O.Y, FMath::Min((float)Src.O.Z, Ceiling) - 8.f);
			for (const FVector& D : Src.Dirs)
			{
				for (const float Side : {-1.f, 1.f})
				{
					const FVector Lat = D.IsNearlyZero() ? FVector(0, Side, 0) : FVector(-D.Y, D.X, 0.f) * Side;
					FVector Ret = Returns.Num() ? Returns[0] : O;
					for (const FVector& R : Returns)
					{
						if (FVector::DistSquared2D(R, O + D * 200.f) < FVector::DistSquared2D(Ret, O + D * 200.f))
						{
							Ret = R;
						}
					}
					FPath P;
					P.UnitId = UnitId;
					if (D.IsNearlyZero())  // straight down, then spreading out across the floor
					{
						P.Points = {O, Clamp(O + FVector(0, 0, -120)), Clamp(O + Lat * 60.f + FVector(0, 0, -(O.Z - 60.f))),
						            Clamp(O + Lat * 140.f + FVector(0, 0, -(O.Z - 110.f))), Clamp(FMath::Lerp(O, Ret, 0.6f) + FVector(0, 0, -100)),
						            Clamp(Ret - FVector(0, 0, 50)), Clamp(Ret)};
					}
					else  // horizontal throw along the ceiling, dropping into the occupied zone, back to the return
					{
						P.Points = {O, Clamp(O + D * 90.f + Lat * 10.f + FVector(0, 0, -6)), Clamp(O + D * 200.f + Lat * 35.f + FVector(0, 0, -40)),
						            Clamp(FVector(O.X, O.Y, 0) + D * 240.f + Lat * 55.f + FVector(0, 0, 130)),
						            Clamp(FVector(O.X, O.Y, 0) + D * 150.f + Lat * 70.f + FVector(0, 0, 60)),
						            Clamp(FMath::Lerp(FVector(O.X, O.Y, 0), FVector(Ret.X, Ret.Y, 0), 0.6f) + FVector(0, 0, 110)),
						            Clamp(Ret - FVector(0, 0, 45)), Clamp(Ret)};
					}
					for (int32 i = 1; i < P.Points.Num(); ++i)
					{
						P.Length += FVector::Dist(P.Points[i - 1], P.Points[i]);
					}
					P.First = Xf.Num();
					P.Count = FMath::Clamp(FMath::RoundToInt(P.Length / 55.f), 6, 18);
					for (int32 k = 0; k < P.Count; ++k)
					{
						Xf.Add(FTransform(FQuat::Identity, P.Points[0], FVector(DotScale)));
					}
					Paths.Add(MoveTemp(P));
				}
			}
		}
	}
	Dots->AddInstances(Xf, false, true, false);
	for (int32 i = 0; i < Xf.Num(); ++i)
	{
		Dots->SetCustomDataValue(i, 0, 0.75f);
		Dots->SetCustomDataValue(i, 1, 0.75f);
		Dots->SetCustomDataValue(i, 2, 0.78f);
	}
	Dots->MarkRenderStateDirty();
}

void ATwinAirflowActor::Tick(float DeltaSeconds)
{
	Super::Tick(DeltaSeconds);
	if (!bShown || Xf.Num() == 0)
	{
		return;
	}
	Clock += DeltaSeconds;
	const ATwinViewerDirector* D = Director.Get();
	Visible = 0;
	for (const FPath& P : Paths)
	{
		const FTwinHvacReading* R = D ? D->ReadingFor(P.UnitId) : nullptr;
		const bool bOff = R && !R->bOn;
		const float Fan = (R && !FMath::IsNaN(R->Fan)) ? FMath::Max(0.25f, R->Fan) : 0.8f;
		const float Supply = R ? R->SupplyC : NAN;
		const float Return = R ? R->ReturnC : NAN;
		const float Cycle = P.Length / (BaseSpeed * Fan);
		for (int32 k = 0; k < P.Count; ++k)
		{
			const float U = FMath::Fractional((float)k / P.Count + (float)(Clock / FMath::Max(Cycle, 1.f)));
			const FVector Pos = CatmullRom(P.Points, U);
			const float Fade = FMath::Min(1.f, FMath::Min(U, 1.f - U) * 8.f);
			const float Scale = bOff ? 0.f : DotScale * (0.55f + 0.45f * Fade);
			Xf[P.First + k] = FTransform(FQuat::Identity, Pos, FVector(Scale));
			const float Temp = (FMath::IsNaN(Supply) || FMath::IsNaN(Return)) ? NAN : FMath::Lerp(Supply, Return, FMath::Pow(U, 0.7f));
			const FLinearColor C = TemperatureColor(Temp);
			Dots->SetCustomDataValue(P.First + k, 0, C.R);
			Dots->SetCustomDataValue(P.First + k, 1, C.G);
			Dots->SetCustomDataValue(P.First + k, 2, C.B);
			Visible += bOff ? 0 : 1;
		}
	}
	Dots->BatchUpdateInstancesTransforms(0, Xf, true, true, true);
}
